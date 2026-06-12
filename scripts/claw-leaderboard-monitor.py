#!/usr/bin/env python3
"""
ClawBytes Leaderboard Monitor
Watches coding-agent benchmark leaderboards and emits an item when the top
of a board actually moves. Boards: SWE-bench (swebench.com) and the Aider
polyglot leaderboard (aider.chat/docs/leaderboards).

Cheap by design: each board's backing file lives in a GitHub repo, so we
poll the file's git sha via the contents API (tiny call) and only download
and diff when the sha changes. The first sighting of a board records a
baseline and emits nothing.

State file: memory/claw-leaderboard-state.json
"""

import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-leaderboard-state.json"

TOP_N = 3

BOARDS = [
    {
        "key": "swebench-verified",
        "label": "SWE-bench Verified",
        "page": "https://www.swebench.com/",
        "repo": "SWE-bench/swe-bench.github.io",
        "path": "data/leaderboards.json",
        "ref": "master",
        "parser": "swebench",
        "board_name": "Verified",
    },
    {
        "key": "swebench-bash-only",
        "label": "SWE-bench bash-only",
        "page": "https://www.swebench.com/",
        "repo": "SWE-bench/swe-bench.github.io",
        "path": "data/leaderboards.json",
        "ref": "master",
        "parser": "swebench",
        "board_name": "bash-only",
    },
    {
        "key": "aider-polyglot",
        "label": "Aider polyglot leaderboard",
        "page": "https://aider.chat/docs/leaderboards/",
        "repo": "Aider-AI/aider",
        "path": "aider/website/_data/polyglot_leaderboard.yml",
        "ref": "main",
        "parser": "aider",
    },
    {
        # The data CSV is release-dated and rotates (~2x/yr) when LiveBench
        # snapshots a new question set — resolve the latest table_*.csv from
        # the dir listing instead of hardcoding a date.
        "key": "livebench",
        "label": "LiveBench",
        "page": "https://livebench.ai",
        "repo": "LiveBench/livebench.github.io",
        "dir": "public",
        "pattern": ("table_", ".csv"),
        "ref": "main",
        "parser": "livebench",
    },
]


def _github_headers():
    headers = {"User-Agent": "ClawBytes/1.0", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_file_sha(repo, path, ref):
    """Current git sha of a file — tiny metadata call, no content download."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"
    try:
        req = Request(url, headers=_github_headers())
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("sha", "")
    except Exception:
        return ""


def fetch_raw(repo, path, ref):
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        req = Request(url, headers={"User-Agent": "ClawBytes/1.0"})
        with urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_swebench(text, board_name, top_n=TOP_N):
    """Top entries of one named board from swebench leaderboards.json."""
    try:
        boards = json.loads(text).get("leaderboards", [])
    except Exception:
        return []
    for board in boards:
        if board.get("name") != board_name:
            continue
        entries = []
        for entry in board.get("results", []):
            name = (entry.get("name") or "").strip()
            try:
                score = float(entry.get("resolved") or 0)
            except (TypeError, ValueError):
                continue
            if name:
                entries.append((name, score))
        entries.sort(key=lambda pair: -pair[1])
        return entries[:top_n]
    return []


def pick_latest_table(names, prefix, suffix):
    """Lexically-latest matching filename (dates in names sort correctly)."""
    matching = [n for n in names if n.startswith(prefix) and n.endswith(suffix)]
    return max(matching) if matching else None


def resolve_dynamic_path(board):
    """Resolve a rotating-filename board to (path, sha) via one dir listing."""
    url = f"https://api.github.com/repos/{board['repo']}/contents/{board['dir']}?ref={board['ref']}"
    try:
        req = Request(url, headers=_github_headers())
        with urlopen(req, timeout=20) as resp:
            listing = json.loads(resp.read().decode())
        prefix, suffix = board["pattern"]
        latest = pick_latest_table([f.get("name", "") for f in listing], prefix, suffix)
        if not latest:
            return None
        sha = next((f.get("sha", "") for f in listing if f.get("name") == latest), "")
        return f"{board['dir']}/{latest}", sha
    except Exception:
        return None


def parse_livebench(text, top_n=TOP_N):
    """Top entries from a LiveBench table CSV: model column + per-category
    numeric scores; overall = mean of categories (matches the site's math)."""
    entries = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("model") or "").strip()
        values = []
        for key, value in row.items():
            if key == "model" or value in (None, ""):
                continue
            try:
                values.append(float(value))
            except ValueError:
                continue
        if name and values:
            entries.append((name, round(sum(values) / len(values), 1)))
    entries.sort(key=lambda pair: -pair[1])
    return entries[:top_n]


def parse_aider_polyglot(text, top_n=TOP_N):
    """Top entries from aider's polyglot_leaderboard.yml without a yaml dep.

    Entries are '- dirname:' blocks with 'model:' and 'pass_rate_2:' keys.
    """
    entries = []
    for block in re.split(r"\n- ", "\n" + text):
        model = re.search(r"^\s*model:\s*(.+?)\s*$", block, re.MULTILINE)
        rate = re.search(r"^\s*pass_rate_2:\s*([\d.]+)", block, re.MULTILINE)
        if model and rate:
            entries.append((model.group(1).strip().strip('"'), float(rate.group(1))))
    entries.sort(key=lambda pair: -pair[1])
    return entries[:top_n]


def diff_tops(old, new):
    """Classify movement between two top-N lists.

    Returns 'new_leader', 'top3_change', or None. A missing/empty old list is
    a baseline, never news.
    """
    if not new or not old:
        return None
    if new[0][0] != old[0][0]:
        return "new_leader"
    if [name for name, _ in new] != [name for name, _ in old]:
        return "top3_change"
    return None


def build_item(board, change, tops, now_iso):
    leader, score = tops[0]
    if change == "new_leader":
        title = f"{leader} takes #1 on {board['label']} ({score:.1f}%)"
    else:
        title = f"{board['label']}: top-3 movement"
    ranked = " · ".join(f"{name} ({value:.1f}%)" for name, value in tops)
    day = now_iso[:10].replace("-", "")
    return {
        # Unique per change: postedUrls dedup keys on URL, so a bare page URL
        # would swallow every future movement after the first posted one.
        "id": f"{board['key']}:{day}:{leader}",
        "board": board["label"],
        "title": title,
        "url": f"{board['page']}#{board['key']}-{day}",
        "summary": f"Top {len(tops)}: {ranked}",
        "change": change,
        "found_at": now_iso,
    }


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"shas": {}, "tops": {}, "lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def check_boards(verbose=True):
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []
    raw_cache = {}

    for board in BOARDS:
        if "pattern" in board:
            resolved = resolve_dynamic_path(board)
            if not resolved:
                if verbose:
                    print(f"  ! {board['label']}: could not resolve data file")
                continue
            path, sha = resolved
        else:
            path = board["path"]
            sha = github_file_sha(board["repo"], path, board["ref"])
        file_key = f"{board['repo']}/{path}@{board['ref']}"
        if sha and sha == state["shas"].get(file_key) and board["key"] in state["tops"]:
            if verbose:
                print(f"  = {board['label']}: unchanged (sha match)")
            continue

        if file_key not in raw_cache:
            raw_cache[file_key] = fetch_raw(board["repo"], path, board["ref"])
        text = raw_cache[file_key]
        if not text:
            if verbose:
                print(f"  ! {board['label']}: fetch failed")
            continue

        if board["parser"] == "swebench":
            tops = parse_swebench(text, board["board_name"])
        elif board["parser"] == "livebench":
            tops = parse_livebench(text)
        else:
            tops = parse_aider_polyglot(text)
        if not tops:
            if verbose:
                print(f"  ! {board['label']}: no entries parsed")
            continue

        old = [tuple(pair) for pair in state["tops"].get(board["key"], [])]
        change = diff_tops(old, tops)
        if change:
            item = build_item(board, change, tops, now_iso)
            new_items.append(item)
            if verbose:
                print(f"  🏆 {item['title']}")
        elif verbose:
            status = "baseline recorded" if not old else "no movement"
            print(f"  = {board['label']}: {status} — #1 {tops[0][0]} ({tops[0][1]:.1f}%)")

        state["tops"][board["key"]] = [list(pair) for pair in tops]
        if sha:
            state["shas"][file_key] = sha

    if new_items:
        state["foundItems"] = (state.get("foundItems", []) + new_items)[-100:]
    save_state(state)
    return new_items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor coding-agent leaderboards")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()
    items = check_boards(verbose=not args.quiet)
    print(f"Leaderboards: {len(items)} new movement item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
