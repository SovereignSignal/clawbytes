#!/usr/bin/env bash
set -euo pipefail

cd /opt/claw/content-engine
export PYTHONPATH=/opt/claw/content-engine/src
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

/opt/claw/.venvs/content-engine/bin/python -m claw_content_engine check-repos \
  --clawbytes /opt/claw/clawbytes \
  --modelbytes /opt/claw/modelbytes

