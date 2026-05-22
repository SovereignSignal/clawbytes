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
