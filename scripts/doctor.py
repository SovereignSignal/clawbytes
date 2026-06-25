#!/usr/bin/env python3
"""ClawBytes VM/runtime doctor.

Checks local prerequisites without printing secret values. Intended for VM
cutover and post-deploy smoke checks.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("WORKSPACE", str(REPO_ROOT)))
MEMORY = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))


def check_path(path: Path, *, writable: bool = False) -> dict:
    result = {"path": str(path), "exists": path.exists()}
    if path.exists():
        result["is_dir"] = path.is_dir()
    if writable:
        try:
            path.mkdir(parents=True, exist_ok=True)
            result["exists"] = path.exists()
            result["is_dir"] = path.is_dir()
            probe = path / ".doctor-write-test"
            probe.write_text("ok")
            probe.unlink()
            result["writable"] = True
        except Exception as exc:
            result["writable"] = False
            result["error"] = str(exc)
    return result


def check_command(name: str) -> dict:
    path = shutil.which(name)
    return {"command": name, "found": bool(path), "path": path}


def check_python_compile() -> dict:
    py_files = sorted(str(p) for p in REPO_ROOT.glob("*.py"))
    py_files.extend(str(p) for p in sorted((REPO_ROOT / "scripts").glob("*.py")))
    env = os.environ.copy()
    env.setdefault("PYTHONPYCACHEPREFIX", tempfile.mkdtemp(prefix="clawbytes-pycache-"))
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", *py_files],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return {
        "ok": proc.returncode == 0,
        "files_checked": len(py_files),
        "stderr": proc.stderr[-1000:],
    }


def check_preview() -> dict:
    proc = subprocess.run(
        [sys.executable, "clawbytes_threads.py", "preview", "--category", "ship"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ.copy(),
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_head": proc.stdout[:300],
        "stderr_head": proc.stderr[:300],
    }


def secret_presence() -> dict:
    keys = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHANNEL_ID",
        "GITHUB_TOKEN",
        "CLAUDE_CREDENTIALS",
        "SOV_DM_CHAT_ID",
        "DATABASE_URL",
    ]
    return {key: bool(os.environ.get(key)) for key in keys}


def run() -> dict:
    is_linux = platform.system() == "Linux"
    required_secrets = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]
    checks = {
        "platform": platform.platform(),
        "workspace": str(WORKSPACE),
        "memory": str(MEMORY),
        "paths": {
            "workspace": check_path(WORKSPACE),
            "memory": check_path(MEMORY, writable=True),
        },
        "commands": {
            name: check_command(name)
            for name in ["python3", "git", "bash", "flock", "curl", "gh", "claude"]
        },
        "secrets_present": secret_presence(),
        "compile": check_python_compile(),
        "preview": check_preview(),
    }
    checks["missing_required_secrets"] = [
        key for key in required_secrets if not checks["secrets_present"].get(key)
    ]
    checks["ok"] = (
        checks["paths"]["workspace"].get("exists")
        and checks["paths"]["memory"].get("writable")
        and checks["commands"]["python3"].get("found")
        and checks["commands"]["git"].get("found")
        and (checks["commands"]["flock"].get("found") or not is_linux)
        and not checks["missing_required_secrets"]
        and checks["compile"]["ok"]
        and checks["preview"]["ok"]
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="ClawBytes VM/runtime doctor")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = "ok" if report["ok"] else "needs attention"
        print(f"ClawBytes doctor: {status}")
        print(f"workspace: {report['workspace']}")
        print(f"memory: {report['memory']}")
        print(f"compile: {report['compile']['ok']} ({report['compile']['files_checked']} files)")
        print(f"preview: {report['preview']['ok']}")
        missing = [k for k, present in report["secrets_present"].items() if not present]
        if missing:
            print("missing optional/required env keys: " + ", ".join(missing))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
