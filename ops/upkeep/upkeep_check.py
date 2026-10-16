"""Deterministic daily upkeep check for the SovereignSignal bots.

Read-only checks per bot (GitHub issues/PRs, dependency drift, Railway
deployment status, silent-run detection), a plain-text or JSON report, and
an exception-only ops DM when something needs attention.

    python -m ops.upkeep.upkeep_check [--dry-run] [--config PATH] [--json]
                                      [--no-alert] [--fixtures DIR] [--feed-url URL]

Exit codes: 0 nothing needs attention, 1 something does, 2 internal error.

Every check is a pure function over already-fetched data; all network access
goes through a fetcher object (LiveFetcher, or FixtureFetcher for --fixtures)
so tests never touch the network.

Infra identifiers stay out of the repo: Railway ids come from
UPKEEP_RAILWAY_PROJECTS and the AI Wire base URL from AI_WIRE_BASE_URL.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

try:
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - packaging is optional
    Version = None
    InvalidVersion = Exception

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"
RAILWAY_GRAPHQL = "https://backboard.railway.com/graphql/v2"
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 20
USER_AGENT = "sovereignsignal-upkeep-check/1.0"

# Same banner the clawbytes scheduler puts on every ops DM (scripts/scheduler.py).
OPS_BANNER = "🔧 OPS REPORT — visible only to you, never posted to the channel.\n\n"
# Env vars whose values must never appear in output.
SECRET_ENV_VARS = (
    "RAILWAY_TOKEN", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN",
    "CLAWBYTES_ADMIN_CHAT_ID", "MODELBYTES_ADMIN_CHAT_ID",
)
ADMIN_CHAT_ENV_VARS = ("CLAWBYTES_ADMIN_CHAT_ID", "MODELBYTES_ADMIN_CHAT_ID")
# JSON object keyed by bot name: {"<bot>": {"project_id", "service_id", "environment_id"}}.
RAILWAY_PROJECTS_ENV = "UPKEEP_RAILWAY_PROJECTS"
RAILWAY_ID_KEYS = ("project_id", "service_id", "environment_id")
# Registry base URL; the feed is <base>/feed.xml.
AI_WIRE_BASE_ENV = "AI_WIRE_BASE_URL"

BAD_DEPLOY_STATUSES = {"FAILED", "CRASHED", "REMOVED"}
HEALTHY_DEPLOY_STATUSES = {"SUCCESS", "SLEEPING"}

OK, INFO, ATTENTION, SKIPPED, WARNING = "ok", "info", "attention", "skipped", "warning"


@dataclass
class Finding:
    check: str
    level: str
    message: str
    attention: bool = False


def finding(check: str, level: str, message: str) -> Finding:
    return Finding(check, level, message, attention=(level == ATTENTION))


def check_error(check: str, exc: BaseException | str, flag: bool) -> Finding:
    detail = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return Finding(check, WARNING, f"check error: {detail}", attention=flag)


# --------------------------------------------------------------------------
# env-provided infra identifiers


def railway_projects_from_env(env) -> dict:
    """Parse UPKEEP_RAILWAY_PROJECTS into {bot: {project_id, service_id, environment_id}}.

    Unset or invalid JSON gives {}; an entry missing any id is dropped, so that
    bot's Railway checks are skipped rather than flagged.
    """
    raw = (env.get(RAILWAY_PROJECTS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, ids in data.items():
        if isinstance(ids, dict) and all(isinstance(ids.get(k), str) and ids[k].strip()
                                         for k in RAILWAY_ID_KEYS):
            out[name] = {k: ids[k].strip() for k in RAILWAY_ID_KEYS}
    return out


def ai_wire_base_url(env) -> str:
    return (env.get(AI_WIRE_BASE_ENV) or "").strip().rstrip("/")


def ai_wire_feed_url(env) -> str:
    base = ai_wire_base_url(env)
    return f"{base}/feed.xml" if base else ""


# --------------------------------------------------------------------------
# time helpers


def parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    # Railway log timestamps carry nanoseconds; fromisoformat takes at most 6 digits.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_since(then: datetime, now: datetime) -> float:
    return (now - then).total_seconds() / 3600.0


def _fmt_age(hours: float) -> str:
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


# --------------------------------------------------------------------------
# 1. GitHub issues + PRs


def check_github(issues: list, pulls: list, now: datetime, *,
                 stale_days: int = 7, bug_labels=("bug",)) -> list[Finding]:
    """`issues` is the raw /issues list (which includes PRs); `pulls` is /pulls."""
    bug_set = {b.lower() for b in bug_labels}
    real_issues = [i for i in issues if "pull_request" not in i]
    bugs = [i for i in real_issues
            if any((lbl.get("name") or "").lower() in bug_set for lbl in i.get("labels", []))]
    stale = []
    for pr in pulls:
        age_days = hours_since(parse_iso(pr["created_at"]), now) / 24
        if age_days > stale_days:
            stale.append((pr, age_days))

    out = [finding("github", OK if not (bugs or stale) else INFO,
                   f"{len(real_issues)} open issue(s) ({len(bugs)} bug), {len(pulls)} open PR(s)")]
    for issue in sorted(bugs, key=lambda i: i["number"]):
        out.append(finding("github", ATTENTION,
                           f"bug issue #{issue['number']}: {issue.get('title', '').strip()}"))
    for pr, age in sorted(stale, key=lambda p: p[0]["number"]):
        out.append(finding("github", ATTENTION,
                           f"PR #{pr['number']} open {age:.0f} days (> {stale_days}): "
                           f"{pr.get('title', '').strip()}"))
    return out


# --------------------------------------------------------------------------
# 2. Dependency drift

_REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_SPEC = re.compile(r"(===|==|!=|~=|>=|<=|>|<)\s*([^\s,]+)")


@dataclass
class Dep:
    name: str
    specs: list = field(default_factory=list)  # [(op, version)]
    source: str = ""

    @property
    def pinned(self):
        for op, ver in self.specs:
            if op in ("==", "===") and "*" not in ver:
                return ver
        return None

    def spec_str(self) -> str:
        return ",".join(f"{op}{ver}" for op, ver in self.specs)


def parse_requirement(line: str, source: str = "") -> Dep | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http://", "https://")) or "://" in line:
        return None
    line = line.split(";", 1)[0].strip()  # drop environment markers
    m = _REQ_LINE.match(line)
    if not m:
        return None
    return Dep(m.group(1).lower().replace("_", "-"), _SPEC.findall(m.group(3)), source)


def parse_requirements_txt(text: str, source: str = "requirements.txt") -> list[Dep]:
    return [d for d in (parse_requirement(l, source) for l in text.splitlines()) if d]


def parse_pyproject(text: str, source: str = "pyproject.toml") -> list[Dep]:
    if tomllib is not None:
        data = tomllib.loads(text)
        lines = data.get("project", {}).get("dependencies", []) or []
    else:  # pragma: no cover - minimal fallback for very old Pythons
        m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
        lines = re.findall(r"[\"']([^\"']+)[\"']", m.group(1)) if m else []
    return [d for d in (parse_requirement(l, source) for l in lines) if d]


def parse_dependency_file(path: str, text: str) -> list[Dep]:
    if path.endswith(".toml"):
        return parse_pyproject(text, path)
    return parse_requirements_txt(text, path)


def _version_tuple(v: str) -> tuple:
    parts = []
    for piece in re.split(r"[.\-+]", v):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts) or (0,)


def version_key(v: str):
    if Version is not None:
        try:
            return Version(v)
        except InvalidVersion:
            pass
    return _version_tuple(v)


def compare_versions(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    if type(ka) is not type(kb):  # one side unparseable by packaging
        ka, kb = _version_tuple(a), _version_tuple(b)
    return (ka > kb) - (ka < kb)


def major_of(v: str) -> int:
    return _version_tuple(v)[0]


def check_dependencies(deps: list[Dep], latest: dict, *, flag_errors: bool = True) -> list[Finding]:
    """`latest` maps dep name -> latest PyPI version string, or an Exception."""
    out: list[Finding] = []
    minor_notes: list[str] = []
    up_to_date = 0
    for dep in deps:
        if dep.name not in latest:
            continue
        got = latest[dep.name]
        if isinstance(got, BaseException):
            out.append(check_error("deps", f"PyPI lookup for {dep.name}: {type(got).__name__}: {got}",
                                   flag_errors))
            continue
        pinned = dep.pinned
        if pinned:
            if major_of(got) > major_of(pinned):
                out.append(finding("deps", ATTENTION,
                                   f"major drift: {dep.name}=={pinned} (latest {got})"))
            elif compare_versions(got, pinned) > 0:
                minor_notes.append(f"{dep.name}=={pinned} (latest {got})")
            else:
                up_to_date += 1
            continue
        floors = [v for op, v in dep.specs if op == ">="]
        caps = [v for op, v in dep.specs if op == "<"]
        if floors and compare_versions(got, floors[0]) > 0:
            note = f"{dep.name}{dep.spec_str()} (latest {got}"
            if any(compare_versions(got, c) >= 0 for c in caps):
                note += ", above cap"
            minor_notes.append(note + ")")
        else:
            up_to_date += 1
    checked = sum(1 for d in deps if d.name in latest)
    errors = sum(1 for d in deps if isinstance(latest.get(d.name), BaseException))
    behind = checked - errors - up_to_date
    summary = f"{len(deps)} dep(s), {checked} checked against PyPI, {behind} with a newer release"
    out.insert(0, finding("deps", OK, summary))
    if minor_notes:
        out.append(finding("deps", INFO, "newer available: " + "; ".join(minor_notes)))
    return out


def pypi_latest_version(payload: dict) -> str:
    return payload["info"]["version"]


# --------------------------------------------------------------------------
# 3. Railway deployment status


def latest_deployment(gql: dict) -> dict | None:
    if gql.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in gql["errors"])
        raise RuntimeError(f"Railway API error: {msgs}")
    edges = ((gql.get("data") or {}).get("deployments") or {}).get("edges") or []
    return edges[0]["node"] if edges else None


def check_railway(gql: dict, now: datetime) -> list[Finding]:
    node = latest_deployment(gql)
    if node is None:
        return [finding("railway", ATTENTION, "no deployments found for service")]
    status = (node.get("status") or "UNKNOWN").upper()
    age = ""
    if node.get("createdAt"):
        age = f", created {_fmt_age(hours_since(parse_iso(node['createdAt']), now))} ago"
    msg = f"latest deployment {status}{age}"
    if status in BAD_DEPLOY_STATUSES:
        # first:1 means this IS the newest deployment, so a REMOVED one has no successor.
        return [finding("railway", ATTENTION, msg)]
    return [finding("railway", OK if status in HEALTHY_DEPLOY_STATUSES else INFO, msg)]


def check_railway_logs(gql: dict, now: datetime, max_hours: float) -> list[Finding]:
    if gql.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in gql["errors"])
        raise RuntimeError(f"Railway API error: {msgs}")
    logs = (gql.get("data") or {}).get("deploymentLogs") or []
    stamps = []
    for line in logs:
        try:
            stamps.append(parse_iso(line["timestamp"]))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    if not stamps:
        return [finding("railway-logs", ATTENTION, "no timestamped log lines on latest deployment")]
    age = hours_since(max(stamps), now)
    level = ATTENTION if age > max_hours else OK
    return [finding("railway-logs", level, f"last log line {_fmt_age(age)} ago (limit {max_hours:g}h)")]


# --------------------------------------------------------------------------
# 4. Silent-run detection

_TG_DATE = re.compile(r'tgme_widget_message_date[^>]*>\s*<time[^>]*datetime="([^"]+)"')
_ANY_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')


def parse_telegram_latest(html: str) -> datetime | None:
    stamps = _TG_DATE.findall(html) or _ANY_TIME.findall(html)
    parsed = []
    for s in stamps:
        try:
            parsed.append(parse_iso(s))
        except ValueError:
            continue
    return max(parsed) if parsed else None


def check_telegram(html: str, now: datetime, max_hours: float, channel: str = "") -> list[Finding]:
    latest = parse_telegram_latest(html)
    if latest is None:
        return [finding("telegram", ATTENTION, f"no posts found in t.me/s/{channel} preview")]
    age = hours_since(latest, now)
    level = ATTENTION if age > max_hours else OK
    return [finding("telegram", level,
                    f"last post in @{channel} {_fmt_age(age)} ago (limit {max_hours:g}h)")]


def parse_feed_latest(xml_text: str) -> tuple[datetime | None, int]:
    root = ET.fromstring(xml_text)
    stamps, count = [], 0
    for item in root.iter("item"):
        count += 1
        pub = item.findtext("pubDate")
        if not pub:
            continue
        try:
            dt = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        stamps.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    return (max(stamps) if stamps else None), count


def check_ai_wire_feed(xml_text: str, now: datetime, max_hours: float) -> list[Finding]:
    latest, count = parse_feed_latest(xml_text)
    if latest is None:
        return [finding("ai-wire", ATTENTION, f"registry feed has no dated items ({count} items)")]
    age = hours_since(latest, now)
    level = ATTENTION if age > max_hours else OK
    return [finding("ai-wire", level,
                    f"registry feed: newest item {_fmt_age(age)} ago (limit {max_hours:g}h, all bots)")]


# --------------------------------------------------------------------------
# fetchers


class LiveFetcher:
    """Network access. Tokens come from env; values are never logged."""

    def __init__(self, env=None, session=None):
        import requests  # local import keeps tests and --fixtures network-free

        self.env = os.environ if env is None else env
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        self.github_token = (self.env.get("GITHUB_TOKEN") or "").strip()
        self.railway_token = (self.env.get("RAILWAY_TOKEN") or "").strip()

    @property
    def railway_available(self) -> bool:
        return bool(self.railway_token)

    def _gh_headers(self, raw=False):
        h = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}
        if self.github_token:
            h["Authorization"] = f"Bearer {self.github_token}"
        return h

    def _gh_list(self, url: str, max_pages: int = 5) -> list:
        out, pages = [], 0
        while url and pages < max_pages:
            r = self.http.get(url, headers=self._gh_headers(), timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            out.extend(r.json())
            url = r.links.get("next", {}).get("url")
            pages += 1
        return out

    def github_issues(self, repo: str) -> list:
        return self._gh_list(f"{GITHUB_API}/repos/{repo}/issues?state=open&per_page=100")

    def github_pulls(self, repo: str) -> list:
        return self._gh_list(f"{GITHUB_API}/repos/{repo}/pulls?state=open&per_page=100")

    def github_file(self, repo: str, path: str) -> str | None:
        r = self.http.get(f"{GITHUB_API}/repos/{repo}/contents/{path}",
                          headers=self._gh_headers(raw=True), timeout=HTTP_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        if r.headers.get("Content-Type", "").startswith("application/json"):
            body = r.json()  # some proxies ignore the raw media type
            return base64.b64decode(body.get("content", "")).decode("utf-8")
        return r.text

    def pypi(self, package: str) -> dict:
        r = self.http.get(f"https://pypi.org/pypi/{package}/json", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _railway(self, query: str, variables: dict) -> dict:
        r = self.http.post(RAILWAY_GRAPHQL, json={"query": query, "variables": variables},
                           headers={"Authorization": f"Bearer {self.railway_token}",
                                    "Content-Type": "application/json"},
                           timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def railway_deployments(self, project_id: str, service_id: str, environment_id: str) -> dict:
        q = ("query($p: String!, $s: String!, $e: String!) {"
             " deployments(first: 1, input: {projectId: $p, serviceId: $s, environmentId: $e})"
             " { edges { node { id status createdAt } } } }")
        return self._railway(q, {"p": project_id, "s": service_id, "e": environment_id})

    def railway_logs(self, deployment_id: str, limit: int) -> dict:
        q = ("query($d: String!, $n: Int) {"
             " deploymentLogs(deploymentId: $d, limit: $n) { timestamp message } }")
        return self._railway(q, {"d": deployment_id, "n": limit})

    def telegram_preview(self, channel: str) -> str:
        r = self.http.get(f"https://t.me/s/{channel}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.text

    def ai_wire_feed(self, url: str) -> str:
        r = self.http.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.text


class FixtureFetcher:
    """Offline fetcher over recorded responses. Layout under DIR:

        github/<owner>__<repo>/issues.json, pulls.json, contents/<path>
        pypi/<package>.json
        railway/deployments_<service_id>.json, railway/logs_<deployment_id>.json
        telegram/<channel>.html
        ai_wire/feed.xml

    Railway checks run only if DIR/railway exists (stands in for RAILWAY_TOKEN).
    A missing file raises FileNotFoundError, which the runner reports as a check
    error -- except a missing contents/<path>, which means "file not in repo".
    """

    def __init__(self, root):
        self.root = Path(root)

    @property
    def railway_available(self) -> bool:
        return (self.root / "railway").is_dir()

    def _json(self, *parts):
        return json.loads(self.root.joinpath(*parts).read_text(encoding="utf-8"))

    def _text(self, *parts):
        return self.root.joinpath(*parts).read_text(encoding="utf-8")

    def github_issues(self, repo):
        return self._json("github", repo.replace("/", "__"), "issues.json")

    def github_pulls(self, repo):
        return self._json("github", repo.replace("/", "__"), "pulls.json")

    def github_file(self, repo, path):
        p = self.root / "github" / repo.replace("/", "__") / "contents" / path
        return p.read_text(encoding="utf-8") if p.exists() else None

    def pypi(self, package):
        return self._json("pypi", f"{package}.json")

    def railway_deployments(self, project_id, service_id, environment_id):
        return self._json("railway", f"deployments_{service_id}.json")

    def railway_logs(self, deployment_id, limit):
        return self._json("railway", f"logs_{deployment_id}.json")

    def telegram_preview(self, channel):
        return self._text("telegram", f"{channel}.html")

    def ai_wire_feed(self, url):
        return self._text("ai_wire", "feed.xml")


# --------------------------------------------------------------------------
# runner


def run_bot(bot: dict, cfg: dict, fetcher, now: datetime, railway_ids: dict | None = None) -> list[Finding]:
    """`railway_ids` is this bot's entry from UPKEEP_RAILWAY_PROJECTS, or None."""
    flag = cfg.get("flag_on_check_error", True)
    out: list[Finding] = []
    repo = bot["repo"]

    try:
        out += check_github(fetcher.github_issues(repo), fetcher.github_pulls(repo), now,
                            stale_days=bot.get("stale_pr_days", cfg.get("stale_pr_days", 7)),
                            bug_labels=cfg.get("bug_labels", ["bug"]))
    except Exception as exc:  # noqa: BLE001 - one failing check must not stop the rest
        out.append(check_error("github", exc, flag))

    try:
        deps: list[Dep] = []
        seen: set[str] = set()
        found_any = False
        for path in bot.get("requirements_files", ["requirements.txt"]):
            text = fetcher.github_file(repo, path)
            if text is None:
                continue
            found_any = True
            for d in parse_dependency_file(path, text):
                if d.name not in seen:
                    seen.add(d.name)
                    deps.append(d)
        if not found_any:
            out.append(check_error("deps", "no dependency file found in repo", flag))
        else:
            latest = {}
            for d in deps:
                if d.pinned or any(op == ">=" for op, _ in d.specs):
                    try:
                        latest[d.name] = pypi_latest_version(fetcher.pypi(d.name))
                    except Exception as exc:  # noqa: BLE001
                        latest[d.name] = exc
            out += check_dependencies(deps, latest, flag_errors=flag)
    except Exception as exc:  # noqa: BLE001
        out.append(check_error("deps", exc, flag))

    deployment_id = None
    railway_skip = None
    if not railway_ids:
        railway_skip = f"skipped ({RAILWAY_PROJECTS_ENV} not set for {bot['name']})"
    elif not fetcher.railway_available:
        railway_skip = "skipped (no RAILWAY_TOKEN)"
    if railway_skip:
        out.append(finding("railway", SKIPPED, railway_skip))
    else:
        try:
            gql = fetcher.railway_deployments(*(railway_ids[k] for k in RAILWAY_ID_KEYS))
            out += check_railway(gql, now)
            node = latest_deployment(gql)
            deployment_id = node and node.get("id")
        except Exception as exc:  # noqa: BLE001
            out.append(check_error("railway", exc, flag))

    max_hours = float(bot.get("max_silence_hours", 26))
    channel = bot.get("telegram_channel")
    if channel:
        try:
            out += check_telegram(fetcher.telegram_preview(channel), now, max_hours, channel)
        except Exception as exc:  # noqa: BLE001
            out.append(check_error("telegram", exc, flag))

    if cfg.get("check_railway_logs", True):
        if railway_skip:
            out.append(finding("railway-logs", SKIPPED, railway_skip))
        elif deployment_id:
            try:
                out += check_railway_logs(
                    fetcher.railway_logs(deployment_id, int(cfg.get("railway_log_limit", 50))),
                    now, float(bot.get("log_silence_hours", max_hours)))
            except Exception as exc:  # noqa: BLE001
                out.append(check_error("railway-logs", exc, flag))
        else:
            out.append(finding("railway-logs", SKIPPED, "skipped (no deployment to read logs from)"))
    return out


def run_checks(cfg: dict, fetcher, now: datetime, *, railway_projects: dict | None = None,
               feed_url: str = "") -> dict:
    """`railway_projects` comes from railway_projects_from_env; `feed_url` is the registry RSS."""
    railway_projects = railway_projects or {}
    bots = []
    for bot in cfg["bots"]:
        findings = run_bot(bot, cfg, fetcher, now, railway_projects.get(bot["name"]))
        bots.append({"name": bot["name"],
                     "attention": any(f.attention for f in findings),
                     "findings": findings})
    global_findings: list[Finding] = []
    aw = cfg.get("ai_wire") or {}
    if feed_url:
        try:
            global_findings += check_ai_wire_feed(fetcher.ai_wire_feed(feed_url), now,
                                                  float(aw.get("max_silence_hours", 26)))
        except Exception as exc:  # noqa: BLE001
            global_findings.append(check_error("ai-wire", exc, cfg.get("flag_on_check_error", True)))
    else:
        global_findings.append(finding("ai-wire", SKIPPED,
                                       f"skipped ({AI_WIRE_BASE_ENV} not set and no --feed-url)"))
    attention = any(b["attention"] for b in bots) or any(f.attention for f in global_findings)
    return {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "attention": attention,
            "bots": bots, "global": global_findings}


def _label(f: Finding) -> str:
    if f.attention:
        return "ATTENTION"
    return {OK: "OK", INFO: "INFO", SKIPPED: "SKIPPED", WARNING: "WARNING"}.get(f.level, f.level.upper())


def format_report(result: dict) -> str:
    n = sum(f.attention for b in result["bots"] for f in b["findings"]) + \
        sum(f.attention for f in result["global"])
    lines = [f"Bot upkeep check — {result['generated_at']}",
             f"Overall: {'ATTENTION (%d item%s)' % (n, '' if n == 1 else 's') if n else 'OK'}", ""]
    for bot in result["bots"]:
        lines.append(f"[{bot['name']}] {'ATTENTION' if bot['attention'] else 'OK'}")
        lines += [f"  {_label(f):<9} {f.check}: {f.message}" for f in bot["findings"]]
        lines.append("")
    if result["global"]:
        lines.append("[global]")
        lines += [f"  {_label(f):<9} {f.check}: {f.message}" for f in result["global"]]
    return "\n".join(lines).rstrip() + "\n"


def result_to_json(result: dict) -> dict:
    return {**result,
            "bots": [{**b, "findings": [asdict(f) for f in b["findings"]]} for b in result["bots"]],
            "global": [asdict(f) for f in result["global"]]}


# --------------------------------------------------------------------------
# alerting + secret hygiene


def redact(text: str, env=None) -> str:
    env = os.environ if env is None else env
    for name in SECRET_ENV_VARS:
        value = (env.get(name) or "").strip()
        if len(value) >= 4:
            text = text.replace(value, f"<{name}>")
    # Infra identifiers are not secrets, but they are kept out of public logs too.
    base = ai_wire_base_url(env)
    if base:
        text = text.replace(base, f"<{AI_WIRE_BASE_ENV}>")
    for ids in railway_projects_from_env(env).values():
        for key, value in ids.items():
            if len(value) >= 4:
                text = text.replace(value, f"<railway {key}>")
    return text


def admin_chat_id(env) -> tuple[str, str]:
    for name in ADMIN_CHAT_ENV_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return name, value
    return "", ""


def alert_text(report: str) -> str:
    return (OPS_BANNER + report)[:4000]


def send_telegram_alert(text: str, env) -> tuple[bool, str]:
    """Telegram sendMessage to the admin chat. Never raises; never returns secrets."""
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_var, chat_id = admin_chat_id(env)
    if not token or not chat_id:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token),
                                  ("CLAWBYTES_ADMIN_CHAT_ID/MODELBYTES_ADMIN_CHAT_ID", chat_id)) if not v]
        return False, f"alert skipped (no {', '.join(missing)})"
    try:
        import requests

        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text,
                                "disable_web_page_preview": "true"},
                          timeout=30)
        ok = bool(r.json().get("ok")) if r.ok else False
        return ok, (f"alert sent to admin chat ({chat_var})" if ok
                    else f"alert failed (Telegram HTTP {r.status_code})")
    except Exception as exc:  # noqa: BLE001 - alert delivery must never raise
        return False, f"alert failed ({type(exc).__name__})"


# --------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ops.upkeep.upkeep_check", description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="run read-only checks, print the report and the alert that WOULD be sent; send nothing")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="config JSON (default: ops/upkeep/config.json)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of plain text")
    p.add_argument("--no-alert", action="store_true", help="never send the admin DM")
    p.add_argument("--fixtures", metavar="DIR", help="offline run over recorded responses (implies no alert)")
    p.add_argument("--feed-url", metavar="URL",
                   help=f"AI Wire registry RSS (default: ${AI_WIRE_BASE_ENV}/feed.xml; skipped if neither is set)")
    return p


def main(argv=None, *, fetcher=None, sender=send_telegram_alert, now=None, env=None,
         stdout=None, stderr=None) -> int:
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)

    def emit(text, stream=stdout):
        stream.write(redact(text, env))
        stream.flush()

    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if fetcher is None:
            fetcher = FixtureFetcher(args.fixtures) if args.fixtures else LiveFetcher(env)
        now = now or datetime.now(timezone.utc)
        result = run_checks(cfg, fetcher, now, railway_projects=railway_projects_from_env(env),
                            feed_url=args.feed_url or ai_wire_feed_url(env))
        report = format_report(result)

        alert = {"needed": result["attention"], "sent": False, "status": "nothing to alert"}
        text = alert_text(report)
        if result["attention"]:
            if args.dry_run or args.fixtures:
                alert["status"] = "dry run: alert not sent" if args.dry_run else "fixtures: alert not sent"
                alert["would_send"] = text
            elif args.no_alert:
                alert["status"] = "alerting disabled (--no-alert)"
            else:
                alert["sent"], alert["status"] = sender(text, env)

        if args.json:
            emit(json.dumps({**result_to_json(result), "alert": alert}, indent=2, ensure_ascii=False) + "\n")
        else:
            emit(report)
            if "would_send" in alert:
                emit(f"\n--- alert that WOULD be sent to the admin chat ---\n{text}\n--- end alert ---\n")
            if result["attention"]:
                emit(f"\n{alert['status']}\n")
        return 1 if result["attention"] else 0
    except Exception as exc:  # noqa: BLE001 - map any internal failure to exit 2
        emit(f"upkeep_check internal error: {type(exc).__name__}: {exc}\n", stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
