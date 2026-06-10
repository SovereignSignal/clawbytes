# Wider Signal Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen ClawBytes ingestion to cover the full coding-harness ecosystem (open and closed source), make source discovery self-updating, and close the audit feedback loop.

**Architecture:** ClawBytes monitors (in `scripts/`) write per-source state JSON to `$CLAWBYTES_MEMORY_DIR`; `clawbytes_threads.py` classifies items into 4 lanes and publishes. We extend the *source lists* (subreddits, RSS feeds, awesome lists, GitHub queries), the *classifier vocabulary*, schedule the existing-but-dormant *discovery* machinery in `scripts/scheduler.py`, and surface the existing `audit` command weekly via the content-engine Slack reporter.

**Tech Stack:** Python 3 stdlib (urllib, no requests in monitors), bash+jq+curl (ecosystem monitor), APScheduler (scheduler), pytest (tests).

**Key invariants (do not violate):**
1. `repo_name_from_feed()` (`clawbytes_threads.py:250`) matches `REPO_PRIORITY` keys as **substrings in dict-insertion order**. More-specific keys (e.g. `"claude agent sdk"`) MUST appear before more-general ones (`"claude"`).
2. `is_relevant()` in `scripts/claw-rss-monitor.py` returns True for any feed whose name contains `"releases"` — release feeds bypass keyword filtering.
3. Tests must set `CLAWBYTES_MEMORY_DIR` to a temp dir **before** importing `clawbytes_threads` (module reads dynamic feeds at import time and would otherwise touch the repo's tracked `memory/`).
4. Work happens on the existing branch `feat/wider-signal-quality`. Commit after each task with the SovereignSignal identity.

---

### Task 1: Test scaffolding for clawbytes_threads

The repo has no top-level tests (only `content-engine/tests/`). Create the scaffold every later task uses.

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`

- [ ] **Step 1: Create conftest that isolates memory dir before import**

`tests/conftest.py`:
```python
"""Shared test setup for clawbytes_threads tests.

CLAWBYTES_MEMORY_DIR must point at a throwaway dir BEFORE clawbytes_threads
is imported: the module resolves MEMORY and ALLOWED_SUBREDDITS at import time
and would otherwise read (and let tests write) the repo's tracked memory/.
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP_MEMORY = tempfile.mkdtemp(prefix="clawbytes-test-memory-")
os.environ["CLAWBYTES_MEMORY_DIR"] = _TMP_MEMORY
os.environ.setdefault("WORKSPACE", str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 2: Create empty `tests/__init__.py`**

```bash
touch tests/__init__.py
```

- [ ] **Step 3: Verify the module imports cleanly under the scaffold**

Run: `python3 -m pytest tests/ --collect-only -q`
Expected: `no tests ran` / collects 0 items, **no import errors**.

If `pytest` is missing: `python3 -m pip install pytest`.

- [ ] **Step 4: Commit**

```bash
git add tests/__init__.py tests/conftest.py
git commit -m "test: add isolated test scaffold for clawbytes_threads

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Realign subreddits to the harness space

Replace the legacy general-AI subreddits (homelab, singularity, artificial, MachineLearning) with where harness operators actually talk.

**Files:**
- Modify: `scripts/claw-reddit-monitor.py:24-60` (SUBREDDITS)
- Modify: `clawbytes_threads.py:121` (ALLOWED_SUBREDDITS)
- Test: `tests/test_classify_reddit.py`

- [ ] **Step 1: Write the failing test**

`tests/test_classify_reddit.py`:
```python
from datetime import datetime, timezone

import clawbytes_threads as ct


def _item(subreddit, title="Claude Code 2.0 subagent orchestration deep dive",
          score=120, comments=45):
    return {
        "id": "t3_test1",
        "subreddit": subreddit,
        "title": title,
        "url": f"https://www.reddit.com/r/{subreddit}/comments/test1/",
        "score": score,
        "comments": comments,
        "found_at": datetime.now(timezone.utc).isoformat(),
    }


def test_new_harness_subreddits_are_allowed():
    for sub in ["claudeai", "claudecode", "cursor", "chatgptcoding", "ai_agents", "mcp"]:
        assert sub in ct.ALLOWED_SUBREDDITS, f"{sub} missing from ALLOWED_SUBREDDITS"


def test_dropped_subreddits_are_rejected():
    for sub in ["homelab", "singularity", "artificial", "machinelearning"]:
        assert ct.classify_reddit(_item(sub)) is None


def test_classify_reddit_accepts_claudecode_post():
    candidate = ct.classify_reddit(_item("ClaudeCode"))
    assert candidate is not None
    assert "community" in candidate["categories"] or "read" in candidate["categories"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_classify_reddit.py -v`
Expected: FAIL — `claudeai missing from ALLOWED_SUBREDDITS` (and homelab not rejected).

- [ ] **Step 3: Update ALLOWED_SUBREDDITS in clawbytes_threads.py**

Replace line 121:
```python
ALLOWED_SUBREDDITS = {"openclaw", "selfhosted", "localllama", "machinelearning", "artificial", "homelab", "singularity"} | _load_dynamic_subreddits()
```
with:
```python
ALLOWED_SUBREDDITS = {
    "openclaw", "selfhosted", "localllama",
    # Harness-space subs (2026-06 widening)
    "claudeai", "claudecode", "cursor", "chatgptcoding", "ai_agents", "mcp",
} | _load_dynamic_subreddits()
```

- [ ] **Step 4: Update SUBREDDITS in scripts/claw-reddit-monitor.py**

Replace the entire `SUBREDDITS = [...]` block (lines 24-60) with:
```python
SUBREDDITS = [
    {
        "name": "openclaw",
        "url": "https://www.reddit.com/r/openclaw/hot.json?limit=15",
        "type": "hot"
    },
    {
        "name": "ClaudeAI",
        "url": "https://www.reddit.com/r/ClaudeAI/hot.json?limit=15",
        "type": "hot"
    },
    {
        "name": "ClaudeCode",
        "url": "https://www.reddit.com/r/ClaudeCode/hot.json?limit=15",
        "type": "hot"
    },
    {
        "name": "cursor",
        "url": "https://www.reddit.com/r/cursor/hot.json?limit=10",
        "type": "hot"
    },
    {
        "name": "ChatGPTCoding",
        "url": "https://www.reddit.com/r/ChatGPTCoding/hot.json?limit=10",
        "type": "hot"
    },
    {
        "name": "AI_Agents",
        "url": "https://www.reddit.com/r/AI_Agents/hot.json?limit=10",
        "type": "hot"
    },
    {
        "name": "mcp",
        "url": "https://www.reddit.com/r/mcp/hot.json?limit=10",
        "type": "hot"
    },
    {
        "name": "LocalLLaMA",
        "url": "https://www.reddit.com/r/LocalLLaMA/search.json?q=openclaw+OR+claude+code+OR+codex+OR+coding+agent&sort=new&limit=10",
        "type": "search"
    },
    {
        "name": "selfhosted",
        "url": "https://www.reddit.com/r/selfhosted/search.json?q=openclaw+OR+ai+agent+OR+llm+agent&sort=new&limit=10",
        "type": "search"
    },
]
```
(Drops MachineLearning, artificial, homelab, singularity. Hot feeds on busy subs are gated by the existing `passes_quality_filter` score≥30/comments≥15 thresholds.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_classify_reddit.py -v`
Expected: 3 PASS

- [ ] **Step 6: Smoke-run the monitor against a temp memory dir**

Run: `CLAWBYTES_MEMORY_DIR=/tmp/cb-task2 python3 scripts/claw-reddit-monitor.py --quiet`
Expected: exits 0; summary line shows ≥7/9 subreddits accessible (some may rate-limit; that's tolerable, none should 404 as nonexistent subs).

- [ ] **Step 7: Commit**

```bash
git add scripts/claw-reddit-monitor.py clawbytes_threads.py tests/test_classify_reddit.py
git commit -m "feat: realign Reddit intake to harness-space subreddits

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Refresh classifier vocabulary

The Read-lane router and the RSS relevance gate predate harness-era vocabulary ("subagent", "skills", "hooks", "harness"…). Without this, wider sources get filtered back out.

**Files:**
- Modify: `clawbytes_threads.py:148-151` (READ_TERMS)
- Modify: `scripts/claw-rss-monitor.py:72-100` (RELEVANCE_KEYWORDS)
- Test: `tests/test_classify_rss.py`

- [ ] **Step 1: Write the failing test**

`tests/test_classify_rss.py`:
```python
from datetime import datetime, timezone

import clawbytes_threads as ct


def _rss_item(feed, title, high_signal=True):
    return {
        "feed": feed,
        "title": title,
        "link": "https://example.com/post",
        "published": datetime.now(timezone.utc).isoformat(),
        "high_signal": high_signal,
    }


def test_harness_vocab_routes_to_read():
    # Title deliberately avoids "agent"/"mcp" substrings so it does NOT match
    # the pre-widening READ_TERMS (note: "subagent" would match "agent").
    item = _rss_item("Simon Willison", "Skills and hooks: harness context engineering patterns")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"


def test_copilot_blog_routes_to_read():
    item = _rss_item("GitHub Copilot Changelog", "GitHub Copilot CLI adds slash commands")
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "read"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_classify_rss.py -v`
Expected: BOTH tests FAIL with `candidate is None` — neither title matches any pre-widening READ_TERMS entry (the titles deliberately avoid "agent", "mcp", "workflow", etc.).

- [ ] **Step 3: Update READ_TERMS in clawbytes_threads.py**

Replace lines 148-151:
```python
READ_TERMS = [
    "agent", "agentic", "workflow", "memory", "mcp", "security",
    "claude code", "codex", "openclaw",
]
```
with:
```python
READ_TERMS = [
    "agent", "agentic", "workflow", "memory", "mcp", "security",
    "claude code", "codex", "openclaw",
    # Harness-era vocabulary (2026-06 widening)
    "subagent", "harness", "skills", "hooks", "computer use",
    "context engineering", "coding agent", "copilot", "cursor",
    "gemini cli", "windsurf", "aider", "agent sdk",
]
```

- [ ] **Step 4: Update RELEVANCE_KEYWORDS in scripts/claw-rss-monitor.py**

Append inside the `RELEVANCE_KEYWORDS` list (after the "Infrastructure" group, before the closing `]`):
```python
    # Harness ecosystem (2026-06 widening). Substring-matched — only
    # unambiguous tokens here (bare "amp"/"cline"/"zed" match inside
    # ordinary words). Release feeds bypass this gate entirely.
    "subagent", "agent harness", "coding harness", "windsurf",
    "roo code", "claude agent sdk", "agent sdk", "computer use",
    "context engineering", "mcp server", "github copilot",
    "zed editor", "warp terminal", "openhands", "smolagents",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_classify_rss.py tests/test_classify_reddit.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add clawbytes_threads.py scripts/claw-rss-monitor.py tests/test_classify_rss.py
git commit -m "feat: refresh classifier vocab for harness-era signal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Add harness + SDK release feeds and repo priorities

Closed-source harnesses announce via changelogs; open-source ones via GitHub releases. Release feeds bypass the relevance gate, so adding feeds + priorities is the whole job.

**Files:**
- Modify: `scripts/claw-rss-monitor.py:25-69` (RSS_FEEDS)
- Modify: `clawbytes_threads.py:86-105` (REPO_PRIORITY), `clawbytes_threads.py:258-278` (display_repo_name)
- Test: `tests/test_release_feeds.py`

- [ ] **Step 1: Validate candidate feed URLs before adding any**

Run:
```bash
for url in \
  "https://github.blog/changelog/label/copilot/feed/" \
  "https://github.com/Aider-AI/aider/releases.atom" \
  "https://github.com/cline/cline/releases.atom" \
  "https://github.com/RooCodeInc/Roo-Code/releases.atom" \
  "https://github.com/block/goose/releases.atom" \
  "https://github.com/All-Hands-AI/OpenHands/releases.atom" \
  "https://github.com/charmbracelet/crush/releases.atom" \
  "https://github.com/QwenLM/qwen-code/releases.atom" \
  "https://github.com/huggingface/smolagents/releases.atom" \
  "https://github.com/anthropics/claude-agent-sdk-python/releases.atom" \
  "https://github.com/anthropics/claude-agent-sdk-typescript/releases.atom" \
  "https://zed.dev/blog.rss" \
  ; do
  code=$(curl -sL -o /tmp/feedcheck -w "%{http_code}" --max-time 15 "$url")
  if [ "$code" = "200" ] && head -c 300 /tmp/feedcheck | grep -q -e "<feed" -e "<rss" -e "<?xml"; then
    echo "OK   $url"
  else
    echo "FAIL($code) $url"
  fi
done
```
Expected: mostly OK. **Any FAIL line: drop that feed from Step 2 and note it in the commit message.** (Likely casualties: the Zed blog URL — if it fails, also try `https://zed.dev/blog/feed.xml` and `https://zed.dev/atom.xml` before dropping.)

- [ ] **Step 2: Append validated feeds to RSS_FEEDS**

In `scripts/claw-rss-monitor.py`, append before the closing `]` of `RSS_FEEDS` (omit any that failed Step 1):
```python
    # Harness ecosystem widening (2026-06)
    {"name": "GitHub Copilot Changelog", "url": "https://github.blog/changelog/label/copilot/feed/", "tags": ["coding-agent", "official"], "high_signal": True},
    {"name": "Aider Releases", "url": "https://github.com/Aider-AI/aider/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Cline Releases", "url": "https://github.com/cline/cline/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Roo Code Releases", "url": "https://github.com/RooCodeInc/Roo-Code/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Goose Releases", "url": "https://github.com/block/goose/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "OpenHands Releases", "url": "https://github.com/All-Hands-AI/OpenHands/releases.atom", "tags": ["releases", "frameworks"]},
    {"name": "Crush Releases", "url": "https://github.com/charmbracelet/crush/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Qwen Code Releases", "url": "https://github.com/QwenLM/qwen-code/releases.atom", "tags": ["releases", "coding-agent"]},
    {"name": "Smolagents Releases", "url": "https://github.com/huggingface/smolagents/releases.atom", "tags": ["releases", "frameworks"]},
    {"name": "Claude Agent SDK Python Releases", "url": "https://github.com/anthropics/claude-agent-sdk-python/releases.atom", "tags": ["releases", "agent-sdk"], "high_signal": True},
    {"name": "Claude Agent SDK TypeScript Releases", "url": "https://github.com/anthropics/claude-agent-sdk-typescript/releases.atom", "tags": ["releases", "agent-sdk"]},
    {"name": "Zed Blog", "url": "https://zed.dev/blog.rss", "tags": ["coding-agent", "official"]},
```

- [ ] **Step 3: Write the failing test for priorities and key ordering**

`tests/test_release_feeds.py`:
```python
from datetime import datetime, timezone

import clawbytes_threads as ct


def test_new_harness_repos_have_priorities():
    for repo in ["claude agent sdk", "openhands", "aider", "cline",
                 "roo code", "goose", "qwen code", "smolagents", "crush"]:
        assert repo in ct.REPO_PRIORITY, f"{repo} missing from REPO_PRIORITY"


def test_claude_agent_sdk_precedes_claude_in_matching():
    # repo_name_from_feed matches REPO_PRIORITY keys as substrings in dict
    # order: "claude agent sdk" must win over "claude" for SDK feeds.
    assert ct.repo_name_from_feed("Claude Agent SDK Python Releases") == "claude agent sdk"
    assert ct.repo_name_from_feed("Claude Code Releases") == "claude"


def test_aider_release_routes_to_ship():
    item = {
        "feed": "Aider Releases",
        "title": "v0.85.0",
        "link": "https://github.com/Aider-AI/aider/releases/tag/v0.85.0",
        "published": datetime.now(timezone.utc).isoformat(),
    }
    candidate = ct.classify_rss(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert candidate["score"] >= ct.REPO_PRIORITY["aider"]
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python3 -m pytest tests/test_release_feeds.py -v`
Expected: FAIL — `claude agent sdk missing from REPO_PRIORITY`.

- [ ] **Step 5: Update REPO_PRIORITY and display_repo_name**

Replace the whole `REPO_PRIORITY` dict (`clawbytes_threads.py:86-105`) with (note: `"claude agent sdk"` deliberately precedes `"claude code action"` and `"claude"` — substring matching is dict-ordered):
```python
REPO_PRIORITY = {
    "openclaw": 100,
    "hermes": 90,
    "ironclaw": 85,
    "moltis": 82,
    "nanoclaw": 80,
    "openfang": 78,
    "picoclaw": 74,
    "codex": 68,
    "claude agent sdk": 67,
    "claude code action": 67,
    "claude": 66,
    "cursor": 64,
    "gemini": 62,
    "opencode": 60,
    "openai-agents": 60,
    "openhands": 58,
    "aider": 58,
    "mcp": 58,
    "cline": 56,
    "vercel-ai": 56,
    "roo code": 55,
    "continue": 54,
    "goose": 54,
    "qwen code": 54,
    "smolagents": 54,
    "e2b": 52,
    "crush": 50,
}
```

In `display_repo_name()` (`clawbytes_threads.py:258-278`), add to the dict:
```python
        "claude agent sdk": "Claude Agent SDK",
        "openhands": "OpenHands",
        "aider": "Aider",
        "cline": "Cline",
        "roo code": "Roo Code",
        "goose": "Goose",
        "qwen code": "Qwen Code",
        "smolagents": "Smolagents",
        "crush": "Crush",
```

- [ ] **Step 6: Run all tests**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 7: Smoke-run the RSS monitor**

Run: `CLAWBYTES_MEMORY_DIR=/tmp/cb-task4 python3 scripts/claw-rss-monitor.py 2>&1 | tail -20`
Expected: exits 0; new feeds appear in the per-feed output without parse errors (HTTP hiccups on individual feeds are tolerable).

- [ ] **Step 8: Commit**

```bash
git add scripts/claw-rss-monitor.py clawbytes_threads.py tests/test_release_feeds.py
git commit -m "feat: track harness and agent-SDK releases across the wider ecosystem

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Widen discovery inputs (awesome lists + GitHub topic queries)

**Files:**
- Modify: `scripts/claw-ecosystem-monitor.sh:~226-233` (discover_github queries)
- Modify: `scripts/claw-ecosystem-monitor.sh:~300-304` (awesome list URLs)

- [ ] **Step 1: Validate the new awesome-list raw URLs**

Run:
```bash
for url in \
  "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md" \
  "https://raw.githubusercontent.com/hesreallyhim/awesome-claude-code/main/README.md" \
  "https://raw.githubusercontent.com/sourcegraph/awesome-code-ai/main/README.md" \
  ; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$url")
  echo "$code $url"
done
```
Expected: `200` for each. Any non-200: try `master` instead of `main` in the URL; if still failing, drop it.

- [ ] **Step 2: Add the validated URLs to discover_awesome_lists**

In `scripts/claw-ecosystem-monitor.sh`, the array inside `discover_awesome_lists()` currently reads:
```bash
        "https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md"
        "https://raw.githubusercontent.com/kyrolabs/awesome-agents/main/README.md"
```
Append after those lines:
```bash
        "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md"
        "https://raw.githubusercontent.com/hesreallyhim/awesome-claude-code/main/README.md"
        "https://raw.githubusercontent.com/sourcegraph/awesome-code-ai/main/README.md"
```

- [ ] **Step 3: Add topic queries to discover_github**

In `discover_github()`, the `queries` array currently ends with:
```bash
        "mcp+agent+framework&sort=updated&per_page=10"
```
Append after it:
```bash
        "topic:coding-agent&sort=stars&per_page=10"
        "topic:mcp-server&sort=stars&per_page=10"
        "topic:ai-agent+created:>2026-01-01&sort=stars&per_page=10"
```

- [ ] **Step 4: Verify the script still parses and discovery runs**

Run: `bash -n scripts/claw-ecosystem-monitor.sh`
Expected: no output (syntax OK).

Run: `CLAWBYTES_MEMORY_DIR=/tmp/cb-task5 WORKSPACE=/tmp/cb-task5-ws bash scripts/claw-ecosystem-monitor.sh --mode discover 2>&1 | tail -15`
Expected: "Discovery complete!" with ≥0 new projects; no jq/curl hard failures. (Note: the script uses GNU `date -d`; on macOS the new-repo threshold branch may warn — acceptable, it runs on Linux in production. If GITHUB_TOKEN is unset, expect rate-limit skips, also acceptable.)

- [ ] **Step 5: Commit**

```bash
git add scripts/claw-ecosystem-monitor.sh
git commit -m "feat: widen discovery to MCP/Claude/code-AI awesome lists and GitHub topics

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Schedule weekly discovery in the Railway scheduler

The discovery machinery exists but nothing invokes it in production. Add a weekly job.

**Files:**
- Modify: `scripts/scheduler.py`

- [ ] **Step 1: Generalize the runner and add the discover job**

In `scripts/scheduler.py`, replace the existing `_run` function (lines 42-50) with:
```python
def _run_cmd(label: str, cmd: list[str]) -> None:
    """Run a subprocess job, logging start/finish; never raises."""
    log.info("START %s: %s", label, " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
        log.info("DONE %s: exit=%s", label, result.returncode)
    except Exception:  # noqa: BLE001 - a job failure must not kill the scheduler
        log.exception("ERROR %s crashed", label)


def _run(label: str, args: list[str]) -> None:
    """Run a clawbytes_threads.py subcommand."""
    _run_cmd(label, [sys.executable, THREADS, *args])
```

After the `autopublish()` function, add:
```python
def discover() -> None:
    """Weekly source discovery. New repos land in claw-ecosystem-sources.json
    (merged into release checks by get_all_repos) and new feeds/subreddits in
    clawbytes-dynamic-feeds.json (merged by the rss/reddit monitors)."""
    _run_cmd(
        "discover_ecosystem",
        ["bash", str(REPO_ROOT / "scripts" / "claw-ecosystem-monitor.sh"), "--mode", "discover"],
    )
    _run_cmd(
        "discover_feeds",
        [sys.executable, str(REPO_ROOT / "scripts" / "claw-source-discovery.py")],
    )
```

In `main()`, after the `slack_report` add_job line, add:
```python
    # weekly source discovery, Mondays 14:10 UTC (before the day's collects pick it up)
    scheduler.add_job(discover, "cron", day_of_week="mon", hour=14, minute=10, id="discover")
```
And update the final log line to mention it:
```python
    log.info("Scheduled: collect=*:00,30  autopublish=*:05  slack_report=15:30  discover=Mon 14:10 (UTC). Waiting for triggers.")
```

- [ ] **Step 2: Verify job registration without starting the scheduler**

Run:
```bash
python3 - <<'PY'
import sys
sys.path.insert(0, "scripts")
import scheduler as s
from apscheduler.schedulers.background import BackgroundScheduler
sch = BackgroundScheduler(timezone="UTC")
sch.add_job(s.collect, "cron", minute="0,30", id="collect")
sch.add_job(s.discover, "cron", day_of_week="mon", hour=14, minute=10, id="discover")
print("jobs:", [j.id for j in sch.get_jobs()])
PY
```
Expected: `jobs: ['collect', 'discover']` with no import errors.

- [ ] **Step 3: Verify claw-source-discovery.py runs standalone**

Run: `CLAWBYTES_MEMORY_DIR=/tmp/cb-task6 timeout 120 python3 scripts/claw-source-discovery.py 2>&1 | tail -10`
Expected: exits 0 (without BRAVE_API_KEY it should degrade gracefully, discovering little or nothing — that's fine; failure to *run* is not).

- [ ] **Step 4: Commit**

```bash
git add scripts/scheduler.py
git commit -m "feat: run source discovery weekly on the Railway scheduler

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Weekly ingestion-audit report to Slack

Close the feedback loop: the `audit --json` command exists; surface its summary in Slack weekly so dead feeds and vocab gaps become visible.

**Files:**
- Modify: `content-engine/src/claw_content_engine/feed_reports.py`
- Modify: `content-engine/src/claw_content_engine/cli.py`
- Modify: `scripts/scheduler.py`
- Test: `content-engine/tests/test_audit_report.py`

- [ ] **Step 1: Write the failing test**

`content-engine/tests/test_audit_report.py`:
```python
import json
from pathlib import Path

from claw_content_engine import feed_reports


FIXTURE = {
    "memoryDir": "/data/memory",
    "lastCollectedAt": "2026-06-10T14:00:00+00:00",
    "rawItems": 120,
    "statusCounts": {"would_add": 12, "skipped": 80, "rejected": 28},
    "reasonCounts": {"classifier_rejected": 28, "seen_source_key": 70, "posted_url": 10},
    "sourceCounts": {"rss": 60, "reddit": 30, "ecosystem_hn": 20, "hf_papers": 10},
    "laneCounts": {"ship": 5, "watch": 2, "read": 4, "community": 8},
    "unconsumedStateFiles": [{"file": "claw-people-state.json", "items": 7}],
    "currentBundles": {},
    "items": [],
}


def test_audit_report_formats_counts(monkeypatch):
    monkeypatch.setattr(feed_reports, "_run", lambda cmd, cwd: json.dumps(FIXTURE))
    out = feed_reports.clawbytes_audit_report(Path("."))
    assert "ingestion audit" in out
    assert "would_add 12" in out
    assert "classifier_rejected 28" in out
    assert "rss 60" in out
    assert "claw-people-state.json (7)" in out


def test_audit_report_survives_bad_json(monkeypatch):
    monkeypatch.setattr(feed_reports, "_run", lambda cmd, cwd: "Traceback: boom")
    out = feed_reports.clawbytes_audit_report(Path("."))
    assert "audit failed" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest content-engine/tests/test_audit_report.py -v`
Expected: FAIL — `clawbytes_audit_report` doesn't exist.

- [ ] **Step 3: Implement the report builder**

In `content-engine/src/claw_content_engine/feed_reports.py`:

Add `import json` to the imports at the top of the file (it currently imports `re` and `subprocess`; keep alphabetical order).

After `clawbytes_preview_report` (ends line 130), add:
```python
def clawbytes_audit_report(clawbytes: Path, *, python_bin: str = "python3") -> str:
    """Weekly ingestion-audit summary: where raw items die in the pipeline.

    Surfaces classifier rejections (vocab gaps), zero-yield sources (dead
    feeds), and unconsumed state files (orphaned monitors).
    """
    raw = _run([python_bin, "clawbytes_threads.py", "audit", "--json"], clawbytes)
    try:
        report = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return f"*ClawBytes — ingestion audit*\n_audit failed:_\n```{raw[:1000]}```"

    parts = ["*ClawBytes — weekly ingestion audit*"]
    status = report.get("statusCounts", {})
    if status:
        joined = " · ".join(f"{key} {value}" for key, value in sorted(status.items()))
        parts.append(f"_Pipeline: {joined} (of {report.get('rawItems', 0)} raw items)_")
    reasons = report.get("reasonCounts", {})
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
        parts.append("*Drop reasons:* " + " · ".join(f"{key} {value}" for key, value in top))
    sources = report.get("sourceCounts", {})
    if sources:
        ranked = sorted(sources.items(), key=lambda kv: -kv[1])
        parts.append("*Raw items by source:* " + " · ".join(f"{key} {value}" for key, value in ranked))
    lanes = report.get("laneCounts", {})
    if lanes:
        parts.append("*Lane flow:* " + " · ".join(f"{key} {value}" for key, value in lanes.items()))
    unconsumed = report.get("unconsumedStateFiles", [])
    if unconsumed:
        listed = ", ".join(f"{row['file']} ({row['items']})" for row in unconsumed)
        parts.append(f"*Unconsumed state files:* {listed}")
    return "\n\n".join(parts)[:35000]
```

After `send_clawbytes_report` (line 137-138), add:
```python
def send_clawbytes_audit(clawbytes: Path, channel_id: str, *, python_bin: str = "python3") -> tuple[bool, str]:
    return send_text(clawbytes_audit_report(clawbytes, python_bin=python_bin), channel_id=channel_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest content-engine/tests/test_audit_report.py -v`
Expected: 2 PASS

- [ ] **Step 5: Wire the CLI subcommand**

In `content-engine/src/claw_content_engine/cli.py`:

After the `send-clawbytes-report` parser block (lines 36-39), add:
```python
    claw_audit = sub.add_parser("send-clawbytes-audit", help="Post ClawBytes ingestion audit to Slack")
    claw_audit.add_argument("--clawbytes", default=os.environ.get("CLAWBYTES_PATH", "../clawbytes-master"))
    claw_audit.add_argument("--channel-id", default=os.environ.get("CLAWBYTES_SLACK_CHANNEL_ID", ""))
    claw_audit.add_argument("--python-bin", default=os.environ.get("CLAWBYTES_PYTHON", "python3"))
```

After the `send-clawbytes-report` dispatch block (lines 63-66), add:
```python
    if args.command == "send-clawbytes-audit":
        ok, message = send_clawbytes_audit(Path(args.clawbytes), args.channel_id, python_bin=args.python_bin)
        print(message)
        return 0 if ok else 1
```
And extend the existing `from .feed_reports import ...` import in cli.py to include `send_clawbytes_audit`.

- [ ] **Step 6: Add the weekly scheduler job**

In `scripts/scheduler.py`, after `slack_report()`, add:
```python
def audit_report() -> None:
    """Weekly Slack ingestion audit — same gating as the daily lane preview."""
    channel = os.environ.get("CLAWBYTES_SLACK_CHANNEL_ID", "").strip()
    if not channel or not _publish_enabled():
        log.info("SKIP audit_report (channel_set=%s publish=%s)", bool(channel), _publish_enabled())
        return
    env = dict(os.environ)
    ce_src = str(REPO_ROOT / "content-engine" / "src")
    env["PYTHONPATH"] = ce_src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [
        sys.executable, "-m", "claw_content_engine", "send-clawbytes-audit",
        "--clawbytes", str(REPO_ROOT),
        "--channel-id", channel,
        "--python-bin", sys.executable,
    ]
    log.info("START audit_report")
    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, check=False)
        log.info("DONE audit_report: exit=%s", result.returncode)
    except Exception:  # noqa: BLE001 - a job failure must not kill the scheduler
        log.exception("ERROR audit_report crashed")
```
In `main()`, after the discover add_job line, add:
```python
    # weekly ingestion audit to Slack, Mondays 15:45 UTC (after discovery + a collect cycle)
    scheduler.add_job(audit_report, "cron", day_of_week="mon", hour=15, minute=45, id="audit_report")
```

- [ ] **Step 7: End-to-end dry check of the CLI path**

Run:
```bash
CLAWBYTES_MEMORY_DIR=/tmp/cb-task7 PYTHONPATH=content-engine/src python3 -m claw_content_engine send-clawbytes-audit --clawbytes . --channel-id "" 2>&1 | head -5
```
(The memory-dir override keeps `ensure_files()` from touching the repo's tracked `memory/`.)
Expected: it builds the report (audit runs against the repo's memory dir) and then fails to send only because channel-id is empty — output should show the mrkdwn report text or a clean "no channel" send error, NOT a Python traceback.

- [ ] **Step 8: Run the full content-engine test suite**

Run: `python3 -m pytest content-engine/tests/ -v`
Expected: all PASS (including the pre-existing `test_weekly_packet.py`).

- [ ] **Step 9: Commit**

```bash
git add content-engine/src/claw_content_engine/feed_reports.py content-engine/src/claw_content_engine/cli.py content-engine/tests/test_audit_report.py scripts/scheduler.py
git commit -m "feat: weekly ingestion-audit report to Slack

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Widen EDITORIAL_SCOPE.md to the full harness space

The curator/supervisor prompts obey this file; without this edit, wider intake gets re-narrowed at the curation gate.

**Files:**
- Modify: `EDITORIAL_SCOPE.md`

- [ ] **Step 1: Add the harness bullet to "In scope"**

In `EDITORIAL_SCOPE.md`, after the first bullet (the **Claws / OpenClaw and its derivatives** line), insert:
```markdown
- **Coding harnesses and agent CLIs, open or closed source** — Claude Code, Codex CLI, Cursor, Windsurf, GitHub Copilot (agent/CLI modes), Aider, Cline, Roo Code, Goose, OpenHands, Zed (agentic editing), Amp, Devin, Warp, Replit Agent, and credible new entrants. Releases, changelog moves, pricing/limit changes that hit operators, capability shifts, and post-mortems. The harness space is wide and fast-moving — bias toward covering a real move from a smaller harness over silence.
```

- [ ] **Step 2: Add a passing example**

In the "Examples that *would* pass the scope gate" section, add:
```markdown
- "Cursor 1.3 changelog — background agents can now run shell commands in CI. Closed-source, but operators live in it; worth one line."
```

- [ ] **Step 3: Commit**

```bash
git add EDITORIAL_SCOPE.md
git commit -m "docs: widen editorial scope to the full coding-harness space

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `python3 -m pytest tests/ content-engine/tests/ -v`
Expected: all PASS

- [ ] **Step 2: Full collect + audit against a clean temp memory dir**

Run:
```bash
CLAWBYTES_MEMORY_DIR=/tmp/cb-final python3 clawbytes_threads.py collect --run-monitors --summary
CLAWBYTES_MEMORY_DIR=/tmp/cb-final python3 clawbytes_threads.py audit | head -40
```
Expected:
- collect exits 0; summary shows items added.
- audit `sources:` line includes `rss`, `reddit`, `ecosystem_hn` (and `hf_papers` if the ecosystem monitor ran HF).
- audit decision rows mention at least one of the new sources (a `ClaudeAI`/`ClaudeCode`/`cursor` subreddit item, or an `Aider`/`Cline`/`OpenHands`/SDK release) — confirms new intake flows end-to-end.
- No new-feed names appear with hard failures in the collect output.

- [ ] **Step 3: Confirm the working tree state and push**

```bash
git status   # should be clean, all work committed
git log --oneline a2608a3..HEAD   # one commit per task
git push -u origin feat/wider-signal-quality
```

- [ ] **Step 4: Hand off**

Use the superpowers:finishing-a-development-branch skill to decide merge/PR. Note for the PR body: deploy is Railway `claw-bytes` from this repo; the scheduler picks up the new `discover` and `audit_report` jobs on next deploy; no new env vars required (BRAVE_API_KEY/GITHUB_TOKEN remain optional but improve discovery yield).

---

## Out of scope (deliberately)

- **Windsurf/Anthropic-news page watching** — no public feeds; revisit with a Firecrawl monitor as separate infra.
- **X/Twitter, Discord** — no viable unauthenticated API; not worth fragile scraping.
- **npm/PyPI registry polling** — GitHub release feeds for the same SDKs cover publishes in practice (YAGNI).
- **Auto-tuning scoring weights from audit history** — needs longitudinal data the weekly audit will start accumulating; design later with evidence.
