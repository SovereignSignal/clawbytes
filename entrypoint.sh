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
git config --global user.email "${GIT_AUTHOR_EMAIL:-supervisor@clawbytes}"
git config --global user.name "${GIT_AUTHOR_NAME:-ClawBytes Supervisor}"
git config --global --add safe.directory /app

# --- Exec the command ---
exec "$@"
