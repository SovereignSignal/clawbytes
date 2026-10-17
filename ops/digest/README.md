# ops/digest: Tue/Fri ClawBytes issue drafter

Drafts the twice-weekly ClawBytes Substack issue from the public ai-wire registry
feed (`$AI_WIRE_BASE_URL/feed.xml`). It writes markdown and email-safe HTML for an
editor to review. It never posts, sends or emails anything.

```bash
# full draft (needs ANTHROPIC_API_KEY and AI_WIRE_BASE_URL)
python -m ops.digest.draft --md-out issue.md --html-out issue.html

# no Claude call: prints the prompt, renders a deterministic template draft
python -m ops.digest.draft --dry-run --md-out issue.md --html-out issue.html

# offline, from a saved feed
python -m ops.digest.draft --dry-run --feed-file tests/fixtures/ai-wire-feed-sample.xml --date 2026-10-16
```

## Configuration

The registry host is not committed; it comes from the environment.

| Variable | Required? | Where to set | Purpose |
|---|---|---|---|
| `AI_WIRE_BASE_URL` | Yes, unless `--feed-url` or `--feed-file` is given (exits 2 otherwise) | GitHub Actions repo variable; local shell env | Registry base URL. The feed is `<base>/feed.xml`; the base is also the email's "Open the ClawBytes feed" link |
| `ANTHROPIC_API_KEY` | Yes, unless `--dry-run` | GitHub Actions repo secret; local shell env | Claude Messages API key |
| `DIGEST_MODEL` | Optional | GitHub Actions repo variable; local shell env | Claude model (default `claude-sonnet-5-5`; `--model` wins) |

No workflow runs the drafter yet; the "GitHub Actions" entries say where the
values belong if one is added.

| Flag | Meaning |
|---|---|
| `--feed-url URL` | Fetch this feed instead of `$AI_WIRE_BASE_URL/feed.xml` |
| `--feed-file PATH` | Read the feed from a local file; no fetch, `AI_WIRE_BASE_URL` not needed |

Without `AI_WIRE_BASE_URL`, the registry homepage used for fallback links and
the closing call to action is the feed's own channel `<link>`. If that is
missing too, the call to action is left out.

## Window

The window runs from 00:00 UTC on the start date up to 00:00 UTC on the issue date. The issue day itself is not included.

| Flag | Default | Meaning |
|---|---|---|
| `--date YYYY-MM-DD` | today (UTC) | issue date |
| `--days N` | 4 on Tuesday (Fri–Mon), 3 on Friday (Tue–Thu), else 4 | days before the issue date to cover |
| `--since YYYY-MM-DD` | none | start date. Overrides `--days` |

The feed only shows its latest items. If the oldest item in the feed is newer
than the window start, the drafter logs a warning that earlier items may be missing.

## Sections and selection

| Feed `category` | Section |
|---|---|
| `tool_release` | Agents & tools |
| `news`, `paper` that mention agents or coding tools (word match on title and summary) | Agents & tools |
| `model` | Models |
| `repo` | Repos |
| any other `news`/`paper`, `status`, unknown | Also worth a look (optional, small) |

Each section is capped (`--cap-agents 6 --cap-models 5 --cap-repos 6 --cap-also 3`).
Items are ranked newest first, but each source gets one pick before any source repeats. A source is a GitHub `owner/repo`, a Hugging Face org, or a host. This keeps three
releases of the same tool from filling the section.

## Issue structure

Title line, a dateline, a one-paragraph theme opener, then **Agents & tools**, **Models** and **Repos**, an optional **Also worth a look**, and a one-line **Look ahead**. Each
item has a bold headline, a 1–2 sentence body, and `[Read more →](primary source url)`.
The primary source is the URL on the last line of the feed item's description. If there is none, the
link falls back to the item's registry page, then the registry homepage.

## Claude

The drafter calls `https://api.anthropic.com/v1/messages` over plain HTTPS. It sends `ANTHROPIC_API_KEY` in
the `x-api-key` header and never prints the key. The model is `--model`, else `$DIGEST_MODEL`, else
`claude-sonnet-5-5`. Claude gets a JSON list of the selected items and returns JSON copy:
title, opener, a headline and body per item id, and the look-ahead. Links are attached
from the feed rather than written by the model, so every item is linked. The prompt
tells Claude to use only facts from the items, link every item, avoid hype, and never use the
phrase "Where AI is heading".

Deterministic checks run after the call:

- The output must parse, and every non-empty section must exist and cover every item id.
  If it doesn't, the drafter retries once with the problems listed. A second failure exits 2 and writes no files.
- The banned phrase (case-insensitive) triggers the same retry. If it still appears after the retry, it is
  stripped. A final pass also strips it from everything rendered, including feed titles,
  so it can never ship.

Each call logs input/output tokens and an estimated USD cost to stderr, and so does the run total.

## Output

`--md-out` writes markdown. Without it, markdown goes to stdout. `--html-out` writes a single-file HTML
email: table layout, inline styles, no scripts, navy `#1B2A41` with a red `#C8102E` accent. All
text is HTML-escaped, and any link that isn't http(s) is replaced with the registry homepage
(or dropped if there is no homepage).

Exit codes: 0 ok, 1 feed fetch failed or nothing in the window, 2 bad arguments, missing key,
no feed source (`AI_WIRE_BASE_URL` unset and no `--feed-url`/`--feed-file`), or a
Claude/validation failure.

## Tests

`tests/test_digest_draft.py` uses the trimmed real feed in
`tests/fixtures/ai-wire-feed-sample.xml` (registry host and item ids replaced with
placeholders) and mocked HTTP. The tests make no network calls.
