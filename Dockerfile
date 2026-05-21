FROM python:3.11-slim

WORKDIR /app

# System deps: git/gh for supervisor PRs, node/npm for Claude Code, postgres client for state
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    jq \
    bash \
    git \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (for supervisor opening PRs/issues)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Node + Claude Code (headless mode for curator/supervisor)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY . .

# State and memory directories
RUN mkdir -p /app/state /app/memory /app/supervisor-log

# Entrypoint materializes CLAUDE_CREDENTIALS env var to ~/.claude/.credentials.json
# before exec'ing the cron command. Each Railway cron service overrides the CMD.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Default command if no cron service overrides it: just stay alive so Railway
# is happy. Real work is done by per-service startCommands.
CMD ["tail", "-f", "/dev/null"]
