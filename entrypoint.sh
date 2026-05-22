#!/bin/bash
# ClawBytes container entrypoint.
#
# Responsibilities:
# - Materialize CLAUDE_CREDENTIALS env var (raw JSON contents) to
#   ~/.claude/.credentials.json so `claude -p` works headlessly.
# - Materialize GITHUB_TOKEN as gh CLI auth so supervisor can open PRs.
# - Configure git identity for any supervisor commits.
# - Exec the provided command (each Railway cron service sets its own).
#
# Designed to be cheap and idempotent — fires on every container start.

set -e

# --- Claude Code credentials ---
if [ -n "${CLAUDE_CREDENTIALS:-}" ]; then
    mkdir -p "$HOME/.claude"
    echo "$CLAUDE_CREDENTIALS" > "$HOME/.claude/.credentials.json"
    chmod 600 "$HOME/.claude/.credentials.json"
else
    echo "[entrypoint] WARNING: CLAUDE_CREDENTIALS env var not set — claude -p will fail" >&2
fi

# --- GitHub CLI auth (used by supervisor for opening PRs/issues) ---
if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true
fi

# --- Git identity (supervisor commits as Supervisor) ---
git config --global user.email "${GIT_AUTHOR_EMAIL:-supervisor@clawbytes}" 2>/dev/null || \
    echo "[entrypoint] WARNING: could not configure git user.email" >&2
git config --global user.name "${GIT_AUTHOR_NAME:-ClawBytes Supervisor}" 2>/dev/null || \
    echo "[entrypoint] WARNING: could not configure git user.name" >&2
git config --global --add safe.directory /app 2>/dev/null || \
    echo "[entrypoint] WARNING: could not configure git safe.directory" >&2

# --- Legacy Railway code compatibility shim ---
# Some Railway services may still boot an older clawbytes_threads.py from the
# connected branch. Patch only those old signatures at container start so the
# runtime honors shared memory, avoids Notion, and runs the same monitor set.
python3 - <<'PY' || echo "[entrypoint] WARNING: compatibility shim failed" >&2
from pathlib import Path

path = Path("clawbytes_threads.py")
if not path.exists():
    raise SystemExit(0)

text = path.read_text()
original = text

text = text.replace(
    'WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))\n'
    'MEMORY = WORKSPACE / "memory"\n',
    'WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).resolve().parent)))\n'
    'MEMORY = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))\n',
)
text = text.replace(
    'CHANNEL_ID = "-1003850321704"\n',
    'CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "-1003850321704")\n',
)
text = text.replace(
    '# Notion Claws signal integration (must import before WORKSPACE usage).\n'
    '# claw_notion_signals lives in scripts/, so both dirs go on sys.path.\n'
    'sys.path.insert(0, str(Path(__file__).parent / "scripts"))\n'
    'sys.path.insert(0, str(Path(__file__).parent))\n'
    'from claw_notion_signals import enrich_ship_with_notion, find_notion_signals, to_backlog_candidates\n\n',
    'def enrich_ship_with_notion(item: dict) -> dict:\n'
    '    return item\n\n'
    'def find_notion_signals(*args, **kwargs) -> list:\n'
    '    return []\n\n'
    'def to_backlog_candidates(*args, **kwargs) -> list:\n'
    '    return []\n\n',
)
text = text.replace(
    "        'python3 scripts/claw-reddit-monitor.py',\n"
    "        'python3 scripts/claw-moltbook-monitor.py',\n",
    "        'python3 scripts/claw-reddit-monitor.py',\n"
    "        'python3 scripts/claw-hn-monitor.py --quiet',\n"
    "        'python3 scripts/claw-moltbook-monitor.py',\n",
)
text = text.replace(
    '    notion = load_json(MEMORY / "clawbytes-notion-signals.json", [])\n',
    '    notion = []\n',
)

if text != original:
    path.write_text(text)
    print("[entrypoint] patched legacy clawbytes_threads.py runtime")
PY

# --- Legacy Railway cron isolation fallback ---
# Railway cron services do not share local JSON state across containers. If a
# publish job starts in a fresh container, collect inside that same container so
# the publish command has a current backlog to evaluate.
if [[ "${CLAWBYTES_ENTRYPOINT_REFRESH_BEFORE_PUBLISH:-1}" != "0" ]]; then
    command_text="$*"
    if [[ "$command_text" == *"clawbytes_threads.py publish"* ]]; then
        echo "[entrypoint] refreshing ClawBytes sources before publish command" >&2
        python3 clawbytes_threads.py collect --run-monitors --summary || \
            echo "[entrypoint] WARNING: publish-time source refresh failed; continuing to publish command" >&2
    fi
fi

# --- Exec the command ---
exec "$@"
