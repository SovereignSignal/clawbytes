#!/usr/bin/env bash
set -euo pipefail

if [ -f /opt/claw/env/content-engine.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /opt/claw/env/content-engine.env
  set +a
fi

cd /opt/claw/content-engine
export WORKSPACE=/opt/claw/clawbytes
export PYTHONPATH=/opt/claw/content-engine/src
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

/opt/claw/.venvs/content-engine/bin/python -m claw_content_engine send-clawbytes-report \
  --clawbytes "${CLAWBYTES_PATH:-/opt/claw/clawbytes}" \
  --channel-id "${CLAWBYTES_SLACK_CHANNEL_ID:?CLAWBYTES_SLACK_CHANNEL_ID required}" \
  --python-bin "${CLAWBYTES_PYTHON:-/opt/claw/.venvs/clawbytes/bin/python}"

