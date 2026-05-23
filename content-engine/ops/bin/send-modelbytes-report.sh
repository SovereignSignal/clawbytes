#!/usr/bin/env bash
set -euo pipefail

if [ -f /opt/claw/env/content-engine.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /opt/claw/env/content-engine.env
  set +a
fi

cd /opt/claw/content-engine
export PYTHONPATH=/opt/claw/content-engine/src
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

/opt/claw/.venvs/content-engine/bin/python -m claw_content_engine send-modelbytes-report \
  --modelbytes "${MODELBYTES_PATH:-/opt/claw/modelbytes}" \
  --channel-id "${MODELBYTES_SLACK_CHANNEL_ID:?MODELBYTES_SLACK_CHANNEL_ID required}"

