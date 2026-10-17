# ops/highlights: daily highlights-reel collector

Gathers one day's posts from the ClawBytes channels and the AI Wire registry into
one JSON file. It can also ask Claude to draft short reel text from that file. It
only reads public pages and never posts to Telegram, X, Slack or email. Nothing
in the live bot imports it.

```bash
# Today in America/Los_Angeles, JSON to stdout
python -m ops.highlights.collect

# A given day, saved, plus a Claude-drafted reel
python -m ops.highlights.collect --date 2026-10-15 --out day.json \
    --write-reel --reel-out reel.md

# Offline: read the test fixtures, print the writer prompt, never call Claude
AI_WIRE_BASE_URL=https://ai-wire.example.invalid python -m ops.highlights.collect \
    --date 2026-10-14 --fixtures tests/fixtures/highlights --dry-run
```

## Configuration

Nothing here is committed: the registry host and all credentials come from env
vars. Every variable is optional; an input whose variables are missing is
reported as skipped and the rest still run.

| Variable | Required? | Where to set | Purpose |
|---|---|---|---|
| `AI_WIRE_BASE_URL` | Optional; the `registry` input is skipped without it (or pass `--feed-url`) | GitHub Actions repo variable; local shell env | AI Wire registry base URL; the feed is `<base>/feed.xml` |
| `ANTHROPIC_API_KEY` | Only for `--write-reel` without `--dry-run` | GitHub Actions repo secret; local shell env | Claude Messages API key for the reel writer |
| `HIGHLIGHTS_MODEL` | Optional | GitHub Actions repo variable; local shell env | Reel writer model (default `claude-sonnet-5-5`; `--model` wins) |
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | Optional; `gmail` input is skipped without both | Local shell env | Gmail IMAP login (app password, not the account password) |
| `GMAIL_LABEL`, `GMAIL_QUERY` | Optional | Local shell env | Gmail label (default `INBOX`) and extra search terms |
| `X_BEARER_TOKEN` | Optional; `x` input is skipped without it | Local shell env | X v2 recent search |
| `X_HIGHLIGHTS_USERS` | Optional | Local shell env | Comma-separated X handles (default `clawbytes`) |

No workflow runs this collector yet; the "GitHub Actions" entries say where the
values belong if one is added.

## Sources

| Input | What it reads | `source` on items |
|---|---|---|
| `telegram` | `t.me/s/clawbytes`, `t.me/s/ModelBytes`, `t.me/s/interestingrepos` (public previews) | `clawbytes`, `modelbytes`, `interesting-repos` |
| `registry` | AI Wire RSS `$AI_WIRE_BASE_URL/feed.xml`, or `--feed-url`. Skipped if neither is set | `interesting-repos` for `category=repo`, otherwise `registry` |
| `gmail` | Gmail label over IMAP. Disabled unless `GMAIL_USER` and `GMAIL_APP_PASSWORD` are set | `gmail` |
| `x` | X v2 recent search for `X_HIGHLIGHTS_USERS` (default `clawbytes`). Disabled unless `X_BEARER_TOKEN` is set | `x` |
| `jsonfile` | Each `--extra-input FILE` (a list of items, or `{"items": [...]}`) | the item's `source`, or the file stem |

The `gmail` and `x` inputs need real credentials, so they **cannot run in CI**.
Their tests use a fake IMAP client and a mocked HTTP call, and they have not
been tested against live accounts. If you collect Gmail or X items some other
way, write them to a JSON file and merge it with `--extra-input`.

Gmail options: `GMAIL_LABEL` (default `INBOX`) and `GMAIL_QUERY` (Gmail search
syntax, ANDed with the day's date range). Use an app password, not the account
password.

### Day window and pagination

`--date` (default: today in `--tz`, which defaults to `America/Los_Angeles`)
becomes a half-open window from local midnight to the next local midnight,
converted to UTC. For each Telegram channel the collector reads `t.me/s/<slug>`
and then `?before=<oldest id on the page>`. It stops when a page already
reaches back before the day starts, when a page is empty, or after
`--max-pages` pages (default 10). If a later page fails, the items already
found are kept and the source error starts with `partial:`.

## Output

```json
{
  "date": "2026-10-15",
  "tz": "America/Los_Angeles",
  "generated_at": "2026-10-15T23:59:00+00:00",
  "sources": {
    "telegram:clawbytes": {"ok": true, "count": 5, "error": null},
    "registry": {"ok": false, "count": 0, "error": "ConnectionError: ..."},
    "gmail": {"ok": null, "count": 0, "error": null, "skipped": "GMAIL_USER, GMAIL_APP_PASSWORD not set"}
  },
  "duplicates_dropped": 2,
  "items": [
    {"source": "clawbytes", "channel": "t.me/clawbytes", "post_url": "https://t.me/clawbytes/1022",
     "posted_at": "2026-10-15T00:08:29+00:00", "text": "🚢 Ship — 4 items ...",
     "links": ["https://..."], "kind": "ship"}
  ]
}
```

- `ok` is `true` or `false` for inputs that ran, and `null` for inputs that
  were skipped. `skipped` gives the reason.
- `kind` is the ClawBytes lane (`ship`/`watch`/`read`/`community`) when a post
  starts with one. Interesting-repos posts get `repo`, other Telegram posts get
  `post`, and registry items get their category (`repo`, `model`,
  `tool_release`, `news`, `paper`, `status`).
- For registry items, `links[0]` is the primary source URL and `post_url` is the
  registry item page.
- **Dedupe** uses the primary URL, which is `links[0]`, or `post_url` when there
  are no links. Before comparing, the collector lowercases the scheme and host,
  drops `utm_*`/click-id params and the fragment, and strips the trailing slash.
  Inputs run Telegram first, so a channel post beats the registry row for the
  same story. Items are sorted by `posted_at`.
- Exit code: 0 normally. 1 if every input that ran failed, or if `--write-reel`
  had no items to write about. 2 for a bad `--date`/`--tz` or a failed Claude call.

## Reel writer

`--write-reel` sends up to 80 items (each text cut to 500 characters) to the
Claude Messages API over plain HTTPS (`requests`, no SDK). The draft has a hook
line plus 5-8 one-line highlights, and each highlight ends with a link copied
from that item. The draft goes to `--reel-out` (markdown), or to stdout after
the JSON.

- Key: `ANTHROPIC_API_KEY`. It is sent only in the `x-api-key` header and is
  never logged.
- Model: `--model`, else `HIGHLIGHTS_MODEL`, else `claude-sonnet-5-5`.
- Every call logs input/output tokens and an estimated USD cost to stderr, for
  example `highlights writer: model=... input_tokens=4210 output_tokens=380
  est_cost_usd=0.0183`. Prices are in `writer.PRICING`. An unlisted model logs
  `est_cost_usd=unknown`.
- `--dry-run` never calls Claude, even with `--write-reel`. It prints the
  system prompt and a truncated user prompt to stderr. Combine it with
  `--fixtures DIR` to run fully offline.

## Adding an input

Write a class with `name`, `enabled(ctx) -> (bool, reason)` and
`run(ctx) -> list[SourceRun]`, build items with `inputs.make_item(...)`, and
register it in `inputs.INPUTS`. Add it to `DEFAULT_INPUTS` if it should run
without being named in `--inputs`. Report fetch failures on the `SourceRun`
instead of raising.

## Fixtures and tests

`tests/fixtures/highlights/` holds trimmed copies of real `t.me/s` pages, with a
second `_before_<id>` page per channel for pagination, plus a trimmed registry
feed (the registry host is the placeholder `https://ai-wire.example.invalid`).
`--fixtures DIR` maps URLs to those file names; the `registry` input still
needs `AI_WIRE_BASE_URL` or `--feed-url` to run. Tests make no network
calls:

```bash
python -m pytest -q tests/test_highlights_collect.py
```
