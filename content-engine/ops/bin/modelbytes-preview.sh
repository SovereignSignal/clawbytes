#!/usr/bin/env bash
set -euo pipefail

if [ -f /opt/claw/env/modelbytes.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /opt/claw/env/modelbytes.env
  set +a
fi

cd /opt/claw/modelbytes
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

/opt/claw/.venvs/modelbytes/bin/python monitor.py --preview

