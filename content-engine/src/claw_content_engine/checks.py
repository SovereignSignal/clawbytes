from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


def run_checks(clawbytes: Path, modelbytes: Path) -> int:
    failures = 0
    modelbytes_python = _python_for("MODELBYTES_PYTHON", "modelbytes", modelbytes)
    clawbytes_python = _python_for("CLAWBYTES_PYTHON", "clawbytes", clawbytes)
    checks = [
        ("modelbytes pytest", [modelbytes_python, "-m", "pytest", "tests", "-q"], modelbytes),
        ("modelbytes preview", [modelbytes_python, "monitor.py", "--preview"], modelbytes),
        ("clawbytes compile", [clawbytes_python, "-m", "py_compile", *[str(p) for p in _py_files(clawbytes)]], clawbytes),
        ("clawbytes help", [clawbytes_python, "clawbytes_threads.py", "--help"], clawbytes),
        ("clawbytes preview ship", [clawbytes_python, "clawbytes_threads.py", "preview", "--category", "ship"], clawbytes),
        ("clawbytes preview watch", [clawbytes_python, "clawbytes_threads.py", "preview", "--category", "watch"], clawbytes),
        ("clawbytes preview read", [clawbytes_python, "clawbytes_threads.py", "preview", "--category", "read"], clawbytes),
        ("clawbytes preview community", [clawbytes_python, "clawbytes_threads.py", "preview", "--category", "community"], clawbytes),
    ]
    for label, cmd, cwd in checks:
        print(f"\n== {label} ==")
        try:
            env = os.environ.copy()
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            result = subprocess.run(
                cmd,
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            failures += 1
            continue
        if result.stdout:
            _safe_print(result.stdout[-4000:])
        if result.stderr:
            _safe_print(result.stderr[-4000:], stream=sys.stderr)
        if result.returncode != 0:
            failures += 1
            print(f"{label} failed with exit {result.returncode}", file=sys.stderr)
    return failures


def _py_files(root: Path) -> list[Path]:
    return sorted([*root.glob("*.py"), *root.glob("scripts/*.py")])


def _python_for(env_name: str, venv_name: str, repo: Path) -> str:
    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    candidates = [
        Path("/opt/claw/.venvs") / venv_name / "bin" / "python",
        Path("/opt/claw/.venvs") / venv_name / "Scripts" / "python.exe",
        repo / "venv" / "bin" / "python",
        repo / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _safe_print(text: str, stream=sys.stdout) -> None:
    encoding = stream.encoding or "utf-8"
    stream.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    if not text.endswith("\n"):
        stream.write("\n")
