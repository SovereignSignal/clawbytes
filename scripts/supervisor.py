#!/usr/bin/env python3
"""ClawBytes supervisor — daily autonomous custodian.

Runs once per day (2 hours before first publish lane). Performs:
  1. Claude Code auth smoke test
  2. Code health (py_compile + argparse smoke)
  3. Cron health audit via Railway GraphQL
  4. State-file sanity check
  5. Source-list sourcing pass (delegated to Claude with tool access)
  6. Daily summary commit to supervisor-log/
  7. Daily Telegram DM to Sov

Designed for Railway cron service. Logs everything; failures are loud (DM).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from claude_common import (  # noqa: E402
    ClaudeCodeError,
    auth_smoke_test,
    call_claude,
    load_scope,
)

SUPERVISOR_PROMPT_FILE = REPO_ROOT / "docs" / "supervisor-prompt.md"
SUPERVISOR_LOG_DIR = REPO_ROOT / "supervisor-log"
MEMORY = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(REPO_ROOT / "memory")))
DEGRADED_LOG = MEMORY / "degraded_publishes.json"
DISCOVERED_REFS = MEMORY / "discovered_references.json"
CANDIDATE_SOURCES = MEMORY / "candidate_sources.json"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# -- Step 1: Claude auth health ------------------------------------------------


def check_claude_auth(report: dict) -> bool:
    ok, msg = auth_smoke_test(timeout=20)
    report["claude_auth"] = {"ok": ok, "message": msg}
    return ok


# -- Step 2: Code health ------------------------------------------------------


def check_code_health(report: dict) -> dict:
    py_files = sorted(REPO_ROOT.glob("*.py")) + sorted((REPO_ROOT / "scripts").glob("*.py"))
    compile_errors = []
    for f in py_files:
        try:
            subprocess.run(
                ["python3", "-m", "py_compile", str(f)],
                capture_output=True, text=True, check=True, timeout=30,
            )
        except subprocess.CalledProcessError as e:
            compile_errors.append({"file": str(f.relative_to(REPO_ROOT)), "stderr": e.stderr[:500]})

    entry_points = [
        ("clawbytes_threads.py", "status"),
        ("clawbytes_threads.py", "publish --help"),
        ("clawbytes_daily.py", "--help"),
    ]
    arg_errors = []
    for script, args in entry_points:
        sp = REPO_ROOT / script
        if not sp.exists():
            continue
        try:
            subprocess.run(
                ["python3", str(sp)] + args.split(),
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            arg_errors.append({"script": script, "args": args, "error": "timeout"})
        except Exception as e:
            arg_errors.append({"script": script, "args": args, "error": str(e)[:200]})

    result = {
        "files_checked": len(py_files),
        "compile_errors": compile_errors,
        "argparse_errors": arg_errors,
        "ok": not compile_errors and not arg_errors,
    }
    report["code_health"] = result
    return result


# -- Step 3: Cron health via Railway GraphQL ----------------------------------


RAILWAY_GQL_URL = "https://backboard.railway.com/graphql/v2"


def railway_query(query: str, variables: Optional[dict] = None) -> Optional[dict]:
    token = os.environ.get("RAILWAY_TOKEN")
    if not token:
        return None
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = Request(
        RAILWAY_GQL_URL,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"railway GraphQL error: {e}", file=sys.stderr)
        return None


def check_cron_health(report: dict) -> dict:
    project_id = os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("CLAWBYTES_PROJECT_ID")
    if not project_id:
        report["cron_health"] = {"ok": False, "skipped": True, "reason": "no RAILWAY_PROJECT_ID env var"}
        return report["cron_health"]

    q = """
    query($id: String!) {
      project(id: $id) {
        services {
          edges {
            node {
              id name
              serviceInstances {
                edges {
                  node {
                    environmentId cronSchedule startCommand
                    latestDeployment {
                      id status createdAt
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    data = railway_query(q, {"id": project_id})
    if not data:
        report["cron_health"] = {"ok": False, "skipped": True, "reason": "GraphQL call failed"}
        return report["cron_health"]

    services = []
    for edge in (data.get("data", {}).get("project", {}).get("services", {}).get("edges") or []):
        node = edge["node"]
        for si_edge in node.get("serviceInstances", {}).get("edges") or []:
            si = si_edge["node"]
            services.append({
                "service": node["name"],
                "cronSchedule": si.get("cronSchedule"),
                "startCommand": si.get("startCommand"),
                "latestDeployment": si.get("latestDeployment"),
            })

    result = {"services": services, "service_count": len(services), "ok": True}
    report["cron_health"] = result
    return result


# -- Step 4: State-file sanity -----------------------------------------------


def check_state_files(report: dict) -> dict:
    state_files = [
        MEMORY / "claw-rss-state.json",
        MEMORY / "claw-reddit-state.json",
        MEMORY / "claw-security-state.json",
        MEMORY / "claw-moltbook-state.json",
        MEMORY / "claw-hn-state.json",
        MEMORY / "clawbytes-backlog.json",
        MEMORY / "clawbytes-thread-state.json",
    ]
    findings = []
    for f in state_files:
        entry = {"file": f.name, "exists": f.exists()}
        if f.exists():
            try:
                data = json.loads(f.read_text())
                entry["parses"] = True
                if "foundItems" in data:
                    entry["item_count"] = len(data.get("foundItems") or [])
                elif "alerts" in data:
                    entry["item_count"] = len(data.get("alerts") or [])
                elif "items" in data:
                    entry["item_count"] = len(data.get("items") or [])
                entry["last_check"] = data.get("lastCheck")
            except Exception as e:
                entry["parses"] = False
                entry["error"] = str(e)[:200]
        findings.append(entry)
    result = {"files": findings, "ok": all(e.get("exists") and e.get("parses", True) for e in findings)}
    report["state_files"] = result
    return result


# -- Step 5: Source sourcing pass (Claude with tool access) ------------------


def run_sourcing_pass(report: dict, dry_run: bool = False) -> dict:
    """Hand the sourcing job to Claude with read+write tool access to memory/."""
    refs_count = 0
    if DISCOVERED_REFS.exists():
        try:
            refs_count = len(json.loads(DISCOVERED_REFS.read_text()))
        except Exception:
            pass

    if dry_run:
        report["sourcing"] = {"skipped": True, "reason": "dry-run mode", "queued_refs": refs_count}
        return report["sourcing"]

    if not SUPERVISOR_PROMPT_FILE.exists():
        report["sourcing"] = {"skipped": True, "reason": "supervisor prompt missing"}
        return report["sourcing"]

    system_prompt = SUPERVISOR_PROMPT_FILE.read_text() + "\n\n---\n\n# Editorial Scope\n\n" + load_scope()
    user_prompt = (
        "Perform today's sourcing pass. Specifically:\n"
        "1. Read memory/discovered_references.json (drain the queue — process its contents).\n"
        "2. Read memory/clawbytes-backlog.json and memory/degraded_publishes.json for context.\n"
        "3. Read EDITORIAL_SCOPE.md and apply the anchor gate.\n"
        "4. Decide candidate sources to add to memory/candidate_sources.json and "
        "first-class sources to add to memory/claw-ecosystem-sources.json.\n"
        "5. Decide which existing sources to prune.\n"
        "6. Caps: max 5 additions, max 5 removals.\n"
        "7. Write your changes to disk using Edit/Write tools.\n"
        "8. After writing, summarize what you did in a single JSON object with keys: "
        "additions (list), removals (list), graduations (list), notes (string). "
        "Return ONLY that JSON object as your final output."
    )

    try:
        result = call_claude(
            user_prompt,
            system_prompt=system_prompt,
            allowed_tools=["Read", "Write", "Edit", "Bash"],
            timeout=300,
        )
        report["sourcing"] = {
            "ok": True,
            "duration_ms": result.duration_ms,
            "summary_text": result.text[:1000],
            "queued_refs_before": refs_count,
        }
    except ClaudeCodeError as e:
        report["sourcing"] = {"ok": False, "error_kind": e.kind, "error": str(e)}
    return report["sourcing"]


# -- Step 6: Daily summary commit --------------------------------------------


def write_supervisor_log(report: dict) -> Path:
    SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = SUPERVISOR_LOG_DIR / f"{today_key()}.md"
    body = [
        f"# Supervisor run — {today_key()}",
        "",
        f"Started: {report['started']}",
        f"Finished: {report.get('finished', 'unknown')}",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(report, indent=2, default=str),
        "```",
    ]
    log_file.write_text("\n".join(body))
    return log_file


def commit_supervisor_changes(log_path: Path, sourcing_changed: bool, dry_run: bool = False) -> Optional[str]:
    if dry_run:
        return None
    try:
        subprocess.run(["git", "add", str(log_path)], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        if sourcing_changed:
            subprocess.run(["git", "add", "memory/", "EDITORIAL_SCOPE.md"], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=supervisor@clawbytes", "-c", "user.name=ClawBytes Supervisor",
             "commit", "-m", f"supervisor: daily run {today_key()}"],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
        # Push if RAILWAY_TOKEN / GITHUB_TOKEN allows
        push = subprocess.run(["git", "push"], cwd=REPO_ROOT, capture_output=True, text=True)
        return f"committed; push exit={push.returncode}"
    except subprocess.CalledProcessError as e:
        return f"commit failed: {e.stderr[:300]}"


# -- Step 7: Telegram DM -----------------------------------------------------


def telegram_dm(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("SOV_DM_CHAT_ID") or os.environ.get("TELEGRAM_CHANNEL_ID")
    if not token or not chat_id:
        print(f"telegram skipped: missing token or chat_id (chat_id={chat_id!r})", file=sys.stderr)
        return False
    try:
        req = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urlencode({
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }).encode("utf-8"),
        )
        with urlopen(req, timeout=20) as resp:
            json.loads(resp.read().decode("utf-8"))
        return True
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False


def format_dm(report: dict) -> str:
    code_ok = report.get("code_health", {}).get("ok", False)
    auth_ok = report.get("claude_auth", {}).get("ok", False)
    state_ok = report.get("state_files", {}).get("ok", False)
    sourcing = report.get("sourcing", {})
    sourcing_ok = sourcing.get("ok") and not sourcing.get("skipped")
    cron = report.get("cron_health", {})
    cron_ok = cron.get("ok") and not cron.get("skipped")

    all_ok = code_ok and auth_ok and state_ok and (sourcing_ok or sourcing.get("skipped")) and (cron_ok or cron.get("skipped"))
    icon = "🟢" if all_ok else "🟡"
    if not auth_ok:
        icon = "🔴"

    lines = [f"{icon} <b>ClawBytes supervisor — {today_key()}</b>", ""]

    if not auth_ok:
        lines.append("⚠️ <b>CLAUDE CODE AUTH FAILED</b>")
        lines.append(f"  {report['claude_auth']['message']}")
        lines.append("  Action: rotate CLAUDE_CREDENTIALS Railway secret")
        lines.append("")

    if not code_ok:
        errs = report['code_health'].get('compile_errors', [])
        arg_errs = report['code_health'].get('argparse_errors', [])
        lines.append(f"⚠️ <b>Code health: {len(errs)} compile error(s), {len(arg_errs)} argparse error(s)</b>")
        for e in errs[:3]:
            lines.append(f"  {e['file']}")
    else:
        lines.append(f"✅ Code: {report['code_health']['files_checked']} files compile cleanly")

    if state_ok:
        items = [e for e in report['state_files']['files'] if e.get('exists')]
        lines.append(f"✅ State: {len(items)}/{len(report['state_files']['files'])} files present")
    else:
        missing = [e['file'] for e in report['state_files']['files'] if not e.get('exists')]
        lines.append(f"⚠️ State: {len(missing)} missing or unparseable")

    if cron.get("skipped"):
        lines.append(f"⏭️ Cron audit skipped ({cron.get('reason')})")
    elif cron_ok:
        lines.append(f"✅ Cron: {cron['service_count']} services configured")
    else:
        lines.append("⚠️ Cron audit had issues")

    if sourcing.get("skipped"):
        lines.append(f"⏭️ Sourcing skipped ({sourcing.get('reason')})")
    elif sourcing_ok:
        lines.append(f"✅ Sourcing pass: {sourcing.get('duration_ms', 0)}ms")
    else:
        lines.append(f"⚠️ Sourcing failed ({sourcing.get('error_kind', 'unknown')})")

    lines.append("")
    if all_ok:
        lines.append("<i>Nothing needed you today.</i>")
    else:
        lines.append("<i>Review supervisor-log/ in repo for details.</i>")

    return "\n".join(lines)


# -- Orchestrator -----------------------------------------------------------


def run(dry_run: bool = False, skip_sourcing: bool = False, skip_dm: bool = False) -> dict:
    report = {"started": now_utc_iso(), "dry_run": dry_run}

    auth_ok = check_claude_auth(report)
    if not auth_ok:
        # Auth dead — skip Claude-dependent steps, still run deterministic checks
        skip_sourcing = True

    check_code_health(report)
    check_cron_health(report)
    check_state_files(report)

    if not skip_sourcing:
        run_sourcing_pass(report, dry_run=dry_run)
    else:
        report["sourcing"] = {"skipped": True, "reason": "auth dead or --skip-sourcing"}

    report["finished"] = now_utc_iso()

    log_path = write_supervisor_log(report)
    report["log_path"] = str(log_path.relative_to(REPO_ROOT))

    commit_result = commit_supervisor_changes(log_path, sourcing_changed=not skip_sourcing, dry_run=dry_run)
    report["commit"] = commit_result

    if not skip_dm:
        dm_sent = telegram_dm(format_dm(report))
        report["dm_sent"] = dm_sent

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="ClawBytes supervisor — daily autonomous custodian")
    parser.add_argument("--dry-run", action="store_true", help="Don't commit or do sourcing changes; only read + report")
    parser.add_argument("--skip-sourcing", action="store_true", help="Skip the Claude-driven sourcing pass")
    parser.add_argument("--skip-dm", action="store_true", help="Don't send Telegram DM")
    parser.add_argument("--json", action="store_true", help="Print full report JSON to stdout")
    args = parser.parse_args()

    report = run(dry_run=args.dry_run, skip_sourcing=args.skip_sourcing, skip_dm=args.skip_dm)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_dm(report))
        print(f"\nfull log: {report.get('log_path')}")

    # Always exit 0 on a normal run. The supervisor's job is to REPORT issues,
    # not to crash on them. Issues surface via the daily DM + supervisor-log/
    # commit + GitHub issues. A non-zero exit code triggers Railway's
    # "deployment crashed" panic notification, which is noise for "supervisor
    # ran and reported that something else is broken".
    #
    # Reserve non-zero exits for cases where the supervisor itself failed
    # uncaught (which would be a Python exception above this point, not here).
    return 0


if __name__ == "__main__":
    sys.exit(main())
