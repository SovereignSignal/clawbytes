#!/usr/bin/env bash
set -euo pipefail

# Source clawbytes.env FIRST so CLAWBYTES_LLM_* (LLM enrichment creds) reach the
# `clawbytes_threads.py preview` subprocess this report shells out to — without
# them the lanes render generic "New X release" fallbacks. content-engine.env is
# sourced LAST so its Slack creds win on any overlap.
for envf in /opt/claw/env/clawbytes.env /opt/claw/env/content-engine.env; do
  if [ -f "$envf" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$envf"
    set +a
  fi
done

cd /opt/claw/content-engine
export WORKSPACE=/opt/claw/clawbytes
export PYTHONPATH=/opt/claw/content-engine/src
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

/opt/claw/.venvs/content-engine/bin/python -m claw_content_engine send-clawbytes-report \
  --clawbytes "${CLAWBYTES_PATH:-/opt/claw/clawbytes}" \
  --channel-id "${CLAWBYTES_SLACK_CHANNEL_ID:?CLAWBYTES_SLACK_CHANNEL_ID required}" \
  --python-bin "${CLAWBYTES_PYTHON:-/opt/claw/.venvs/clawbytes/bin/python}"
