# AGENTS.md

Start with `CLAUDE.md` (architecture, invariants, ops model) and `README.md` (overview + local dev commands). This file only adds Cursor-Cloud-specific setup/run caveats.

## Cursor Cloud specific instructions

- **What this is:** a Python 3.12 CLI + APScheduler service (ClawBytes signal aggregator) plus a secondary `content-engine/` package. There is **no web server, GUI, or database** — state is JSON on disk. Testing/running is entirely terminal-driven.
- **Use the venv.** Dependencies live in `/workspace/venv` (created by the update script from `requirements.txt` + `pytest`). Run everything with `venv/bin/python …`. There is no global project install.
- **Always `export CLAWBYTES_MEMORY_DIR=/tmp/cb-dev` before running `clawbytes_threads.py` locally.** The module resolves `MEMORY`/feeds at import time; without the override you read and write the repo's tracked `memory/`. (Also noted in `CLAUDE.md`; the test suite handles this itself via `tests/conftest.py`.)
- **Tests:** `venv/bin/python -m pytest tests/ content-engine/tests/ -q` (95 tests, ~1s, no network/secrets needed).
- **Lint:** there is no linter config. The repo convention (per `CLAUDE.md`) is a compile check: `venv/bin/python -m py_compile clawbytes_threads.py scripts/*.py content-engine/src/claw_content_engine/*.py ss_publish/*.py`.
- **Run the app (dry, safe):** `venv/bin/python clawbytes_threads.py collect --run-monitors --summary` fetches from ~9 live source classes over the network (~60s) and classifies into lanes — **no secrets required**. Then inspect with `… status`, `… audit`, and `… preview --category <ship|watch|read|community>`. `preview`/`collect`/`audit` never post.
- **Publishing is gated and off by default.** Actually posting needs `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHANNEL_ID` and `CLAWBYTES_PUBLISH=1` (plus `--send`); none are set here, so all local runs are dry. Do not add publish secrets just to test — use `preview`/`audit`.
- **content-engine** is a separate package rooted at `content-engine/` with `pythonpath=["src"]`. Its pytest works from the repo-root command above; to run its CLI directly use `cd content-engine && PYTHONPATH=src /workspace/venv/bin/python -m claw_content_engine --help`. It expects sibling `clawbytes`/`modelbytes` checkouts for `generate-weekly`, which aren't present here.
- **Ignore `Dockerfile`/`entrypoint.sh`/`railway.toml` for local dev** — those are the Railway deployment path (and include a legacy compatibility shim that rewrites `clawbytes_threads.py` at container start). Don't run the entrypoint locally.
