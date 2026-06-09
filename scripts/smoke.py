#!/usr/bin/env python3
"""Run a safe ClawBytes smoke matrix against temporary state.

Default mode is offline and should not touch repo-local memory or send messages.
Use --network to run real source monitors, still against temporary memory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = ("ship", "watch", "read", "community")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def run_cmd(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int = 120,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def base_env(memory_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "WORKSPACE": str(REPO_ROOT),
            "CLAWBYTES_MEMORY_DIR": str(memory_dir),
            "TELEGRAM_BOT_TOKEN": env.get("TELEGRAM_BOT_TOKEN", "dummy-smoke-token"),
            "TELEGRAM_CHANNEL_ID": env.get("TELEGRAM_CHANNEL_ID", "-1000000000000"),
            "PYTHONPYCACHEPREFIX": env.get(
                "PYTHONPYCACHEPREFIX",
                str(Path(tempfile.gettempdir()) / "clawbytes-pycache"),
            ),
        }
    )
    return env


def all_python_files() -> list[str]:
    skip = {".git", ".venv", "venv", "__pycache__"}
    files = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        files.append(str(path))
    return sorted(files)


def check_python_compile(env: dict[str, str]) -> Check:
    files = all_python_files()
    proc = run_cmd([sys.executable, "-m", "py_compile", *files], env=env, timeout=120)
    return Check(
        "python compile",
        proc.returncode == 0,
        f"{len(files)} files",
        {"stderr": proc.stderr[-1200:]},
    )


def check_shell_syntax(env: dict[str, str]) -> Check:
    candidates = [
        "ops/bin/clawbytes-run.sh",
        "ops/bin/clawbytes-doctor.sh",
        "scripts/claw-ecosystem-monitor.sh",
    ]
    files = [path for path in candidates if (REPO_ROOT / path).exists()]
    skipped = [path for path in candidates if path not in files]
    if not files:
        return Check("shell syntax", True, "no shell files present", {"skipped": skipped})
    proc = run_cmd(["bash", "-n", *files], env=env, timeout=30)
    detail = ", ".join(files)
    if skipped:
        detail += f"; skipped missing: {', '.join(skipped)}"
    return Check("shell syntax", proc.returncode == 0, detail, {"stderr": proc.stderr[-1200:], "skipped": skipped})


def check_doctor(env: dict[str, str]) -> Check:
    proc = run_cmd([sys.executable, "-B", "scripts/doctor.py", "--json"], env=env, timeout=120)
    data: dict[str, Any] = {"stdout": proc.stdout[-1200:], "stderr": proc.stderr[-1200:]}
    try:
        data["report"] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pass
    detail = "ok" if proc.returncode == 0 else "needs attention"
    return Check("doctor", proc.returncode == 0, detail, data)


def check_empty_previews(env: dict[str, str]) -> Check:
    outputs = {}
    failures = []
    for category in CATEGORIES:
        proc = run_cmd(
            [sys.executable, "-B", "clawbytes_threads.py", "preview", "--category", category],
            env=env,
            timeout=30,
        )
        outputs[category] = proc.stdout.strip()
        if proc.returncode != 0 or "Nothing new" not in proc.stdout:
            failures.append(category)
    detail = "all empty previews rendered" if not failures else "failed: " + ", ".join(failures)
    return Check("empty previews", not failures, detail, {"outputs": outputs})


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def seed_fixture_memory(memory_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    now_s = now.isoformat()
    tomorrow = (now + timedelta(days=1)).isoformat()
    write_json(
        memory_dir / "claw-rss-state.json",
        {
            "lastCheck": now_s,
            "foundItems": [
                {
                    "id": "fixture-rss-ship",
                    "feed": "OpenClaw Releases",
                    "title": "OpenClaw 9.9.0",
                    "link": "https://github.com/openclaw/openclaw/releases/tag/v9.9.0",
                    "published": now_s,
                    "summary": "New release",
                },
                {
                    "id": "fixture-rss-read",
                    "feed": "OpenAI Developers",
                    "title": "Agent architecture guide for tool approvals",
                    "link": "https://example.com/agent-architecture",
                    "published": now_s,
                    "summary": "Agent architecture guide",
                    "high_signal": True,
                },
            ],
        },
    )
    write_json(
        memory_dir / "claw-security-state.json",
        {
            "lastCheck": now_s,
            "alerts": [
                {
                    "advisory_id": "GHSA-smoke-1234",
                    "repo": "openclaw/openclaw",
                    "title": "Fixture auth bypass advisory",
                    "url": "https://github.com/openclaw/openclaw/security/advisories/GHSA-smoke-1234",
                    "severity": "HIGH",
                    "found_at": now_s,
                }
            ],
        },
    )
    write_json(
        memory_dir / "claw-hn-state.json",
        {
            "lastCheck": now_s,
            "seenIds": ["fixture-hn-community"],
            "foundItems": [
                {
                    "id": "fixture-hn-community",
                    "title": "Show HN: Fresh agent runtime with local tool sandbox",
                    "url": "https://news.ycombinator.com/item?id=99999999",
                    "score": 42,
                    "comments": 9,
                    "found_at": now_s,
                    "created_at": now_s,
                    "category_hint": "community",
                    "sourceType": "hackernews",
                }
            ],
        },
    )
    write_json(memory_dir / "claw-reddit-state.json", {"lastCheck": now_s, "foundItems": []})
    write_json(memory_dir / "claw-moltbook-state.json", {"lastCheck": now_s, "foundItems": []})
    write_json(
        memory_dir / "clawbytes-backlog.json",
        {
            "items": [
                {
                    "id": "fixture-notion-retire",
                    "url": "https://www.notion.so/fixture",
                    "title": "Legacy Notion fixture",
                    "summary": "Notion should retire",
                    "sourceType": "notion",
                    "primaryCategory": "ship",
                    "categories": ["ship"],
                    "status": "queued",
                    "score": 99,
                    "expiresAt": tomorrow,
                }
            ]
        },
    )


def load_backlog(memory_dir: Path) -> list[dict[str, Any]]:
    path = memory_dir / "clawbytes-backlog.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("items", [])


def check_fixture_lanes(env: dict[str, str], memory_dir: Path) -> Check:
    seed_fixture_memory(memory_dir)
    collect = run_cmd(
        [sys.executable, "-B", "clawbytes_threads.py", "collect", "--summary"],
        env=env,
        timeout=60,
    )
    previews = {}
    failures = []
    for category in CATEGORIES:
        proc = run_cmd(
            [sys.executable, "-B", "clawbytes_threads.py", "preview", "--category", category],
            env=env,
            timeout=30,
        )
        previews[category] = proc.stdout.strip()
        if proc.returncode != 0 or "Nothing new" in proc.stdout:
            failures.append(category)

    backlog = load_backlog(memory_dir)
    counts = Counter(item.get("primaryCategory") for item in backlog if item.get("status") == "queued")
    retired_notion = any(
        item.get("sourceType") == "notion" and item.get("status") == "retired"
        for item in backlog
    )
    if not retired_notion:
        failures.append("notion retirement")

    ok = collect.returncode == 0 and not failures
    detail = "fixture lanes ready" if ok else "failed: " + ", ".join(failures)
    return Check(
        "fixture lanes",
        ok,
        detail,
        {
            "collect_stdout": collect.stdout[-1200:],
            "collect_stderr": collect.stderr[-1200:],
            "queued_counts": dict(counts),
            "retired_notion": retired_notion,
            "previews": previews,
        },
    )


def summarize_memory(memory_dir: Path) -> dict[str, Any]:
    files = sorted(path.name for path in memory_dir.glob("*.json"))
    backlog = load_backlog(memory_dir)
    queued = [item for item in backlog if item.get("status") == "queued"]
    source_counts = Counter(item.get("sourceName") or item.get("sourceType") for item in queued)
    return {
        "files": files,
        "backlog_items": len(backlog),
        "queued_by_category": dict(Counter(item.get("primaryCategory") for item in queued)),
        "top_sources": dict(source_counts.most_common(10)),
    }


def check_network_ingest(env: dict[str, str], memory_dir: Path) -> Check:
    proc = run_cmd(
        [sys.executable, "-B", "clawbytes_threads.py", "collect", "--run-monitors", "--summary"],
        env=env,
        timeout=420,
    )
    summary = summarize_memory(memory_dir)
    detail = f"{summary['backlog_items']} backlog items"
    return Check(
        "network ingest",
        proc.returncode == 0,
        detail,
        {
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-4000:],
            "memory": summary,
        },
    )


def print_report(checks: list[Check], *, json_output: bool, keep_dir: Path | None) -> None:
    report = {
        "ok": all(check.ok for check in checks),
        "checks": [
            {"name": check.name, "ok": check.ok, "detail": check.detail, **({"data": check.data} if json_output else {})}
            for check in checks
        ],
        "memory_dir": str(keep_dir) if keep_dir else None,
    }
    if json_output:
        print(json.dumps(report, indent=2, default=str))
        return

    print("ClawBytes smoke: " + ("ok" if report["ok"] else "needs attention"))
    for check in checks:
        mark = "PASS" if check.ok else "FAIL"
        print(f"- {mark} {check.name}: {check.detail}")
        if not check.ok:
            stderr = check.data.get("stderr") or check.data.get("collect_stderr")
            if stderr:
                print(indent_tail(stderr))
    if keep_dir:
        print(f"memory: {keep_dir}")


def indent_tail(text: str, limit: int = 1200) -> str:
    tail = text[-limit:].strip()
    if not tail:
        return ""
    return "\n".join(f"    {line}" for line in tail.splitlines())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ClawBytes smoke tests against temporary state")
    parser.add_argument("--network", action="store_true", help="Run source monitors against live network endpoints")
    parser.add_argument("--json", action="store_true", help="Print machine-readable report")
    parser.add_argument("--keep-temp", action="store_true", help="Keep the temporary memory directory")
    args = parser.parse_args()

    temp_root = Path(tempfile.mkdtemp(prefix="clawbytes-smoke."))
    temp_roots = [temp_root]
    memory_dir = temp_root / "memory"
    env = base_env(memory_dir)

    try:
        checks = [
            check_python_compile(env),
            check_shell_syntax(env),
            check_doctor(env),
            check_empty_previews(env),
            check_fixture_lanes(env, memory_dir),
        ]

        if args.network:
            network_root = Path(tempfile.mkdtemp(prefix="clawbytes-smoke-network."))
            temp_roots.append(network_root)
            network_memory = network_root / "memory"
            network_env = base_env(network_memory)
            checks.append(check_network_ingest(network_env, network_memory))
            keep_dir = network_memory if args.keep_temp else None
        else:
            keep_dir = memory_dir if args.keep_temp else None

        print_report(checks, json_output=args.json, keep_dir=keep_dir)
        return 0 if all(check.ok for check in checks) else 1
    finally:
        if not args.keep_temp:
            for root in temp_roots:
                shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
