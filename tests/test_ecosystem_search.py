"""GitHub search in the ecosystem discover step sends the existing token.

GITHUB_TOKEN is the token variable the repo already reads. Search must send
it when it is set, keep working when it is not, and on 403/429 back off once
then skip the rest of the search queries without wiping other sources.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claw-ecosystem-monitor.sh"
TOKEN = "test-token-xyz"


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def _env(memory: Path, bindir: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "CLAWBYTES_MEMORY_DIR": str(memory),
        "WORKSPACE": str(memory.parent),
        "GITHUB_TOKEN": "",
        "SHELLOPTS": "allexport",
        "discoveries": "seed",
        "response": "seed",
        "state": "seed",
        "baselines": "seed",
        "releases": "seed",
        "PATH": f"{bindir}:{env.get('PATH', '')}",
    })
    env.update(extra)
    return env


def _run(memory: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--mode", "discover"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_memory(memory: Path) -> None:
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "claw-ecosystem-sources.json").write_text(json.dumps({
        "curated": [{"repo": "acme/already"}],
        "dynamic": [{"repo": "acme/old", "name": "old"}],
        "_meta": {},
    }))
    (memory / "claw-ecosystem-state.json").write_text(json.dumps({
        "lastCheck": None,
        "lastDiscovery": None,
        "lastSeenReleases": {},
        "lastSeenHNStories": [],
        "lastSeenSkills": [],
    }))


def _gh_item(repo: str, description: str, stars: int = 500) -> dict:
    name = repo.split("/", 1)[1]
    return {
        "full_name": repo,
        "name": name,
        "description": description,
        "stargazers_count": stars,
        "html_url": f"https://github.com/{repo}",
        "topics": ["coding-agent"],
        "language": "Python",
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }


def _repo_api(repo: str, description: str, stars: int) -> dict:
    name = repo.split("/", 1)[1]
    return {
        "full_name": repo,
        "name": name,
        "description": description,
        "stargazers_count": stars,
        "html_url": f"https://github.com/{repo}",
        "topics": [],
        "language": "Go",
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "pushed_at": "2026-09-01T00:00:00Z",
        "forks_count": 1,
        "open_issues_count": 0,
    }


def _write_curl(
    bindir: Path,
    log: Path,
    counter: Path,
    routes: list[tuple[str, Path]],
    *,
    search_mode: str,
    ok_search: Path,
    empty_items: Path,
    retry_after: str = "3",
    reset_at: str = "9999999999",
) -> None:
    """search_mode: ok | block_after_first | retry_then_ok.

    Records timestamp, search-call index, Authorization header, and URL.
    Honors curl -D so the script can read Retry-After / X-RateLimit-Reset.
    """
    arms = [f"    {pattern}) cat {_quote(path)} ;;" for pattern, path in routes]
    if search_mode == "ok":
        search_body = "\n".join([
            '    if [ "$n" -eq 1 ]; then',
            f"      cat {_quote(ok_search)}",
            "    else",
            f"      cat {_quote(empty_items)}",
            "    fi",
        ])
    elif search_mode == "block_after_first":
        search_body = "\n".join([
            '    if [ "$n" -eq 1 ]; then',
            f"      cat {_quote(ok_search)}",
            '    elif [ "$n" -eq 2 ] || [ "$n" -eq 3 ]; then',
            '      if [ -n "$hdr_out" ]; then',
            f"        printf 'HTTP/1.1 403 Blocked\\r\\nRetry-After: {retry_after}\\r\\nX-RateLimit-Reset: {reset_at}\\r\\n\\r\\n' > \"$hdr_out\"",
            "      fi",
            "      printf '%s\\n' '{\"message\":\"Blocked\"}'",
            "    else",
            f"      cat {_quote(empty_items)}",
            "    fi",
        ])
    elif search_mode == "retry_then_ok":
        search_body = "\n".join([
            '    if [ "$n" -eq 1 ]; then',
            '      if [ -n "$hdr_out" ]; then',
            f"        printf 'HTTP/1.1 403 Blocked\\r\\nRetry-After: {retry_after}\\r\\nX-RateLimit-Reset: {reset_at}\\r\\n\\r\\n' > \"$hdr_out\"",
            "      fi",
            "      printf '%s\\n' '{\"message\":\"Blocked\"}'",
            '    elif [ "$n" -eq 2 ]; then',
            f"      cat {_quote(ok_search)}",
            "    else",
            f"      cat {_quote(empty_items)}",
            "    fi",
        ])
    else:
        raise AssertionError(search_mode)

    script = "\n".join([
        "#!/bin/sh",
        f"log={_quote(log)}",
        f"counter={_quote(counter)}",
        'url=""',
        'auth=""',
        'hdr_out=""',
        'prev=""',
        'for arg in "$@"; do',
        '  if [ "$prev" = "-D" ]; then hdr_out="$arg"; fi',
        '  if [ "$prev" = "-H" ]; then',
        '    case "$arg" in',
        '      Authorization:*) auth="$arg" ;;',
        "    esac",
        "  fi",
        '  case "$arg" in',
        '    http://*|https://*) url="$arg" ;;',
        "  esac",
        '  prev="$arg"',
        "done",
        'n="-"',
        'case "$url" in',
        "  *search/repositories*)",
        '    n=0',
        '    if [ -f "$counter" ]; then n=$(cat "$counter"); fi',
        '    n=$((n + 1))',
        '    printf "%s\\n" "$n" > "$counter"',
        "    ;;",
        "esac",
        'printf "%s\\t%s\\t%s\\t%s\\n" "$(date +%s.%N)" "$n" "$auth" "$url" >> "$log"',
        'case "$url" in',
        "  *search/repositories*)",
        search_body,
        "    ;;",
        *arms,
        "  *) printf '%s\\n' '{}' ;;",
        "esac",
        "",
    ])
    curl = bindir / "curl"
    curl.write_text(script)
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)


def _fixtures(tmp_path: Path) -> dict[str, Path]:
    search = tmp_path / "search.json"
    search.write_text(json.dumps({"items": [_gh_item("acme/fromsearch", "found via search")]}))
    empty_items = tmp_path / "empty-items.json"
    empty_items.write_text('{"items":[]}\n')
    awesome_meta = tmp_path / "awesome-meta.json"
    awesome_meta.write_text(json.dumps(_repo_api("acme/fromawesome", "listed", 200)))
    hn_meta = tmp_path / "hn-meta.json"
    hn_meta.write_text(json.dumps(_repo_api("acme/fromhn", "discussed", 80)))
    awesome_md = tmp_path / "awesome.md"
    awesome_md.write_text("# Agents\n- https://github.com/acme/fromawesome\n")
    hn_hits = tmp_path / "hn.json"
    hn_hits.write_text(json.dumps({"hits": [{"url": "https://github.com/acme/fromhn"}]}))
    empty_hits = tmp_path / "empty-hits.json"
    empty_hits.write_text('{"hits":[]}\n')
    empty_md = tmp_path / "empty.md"
    empty_md.write_text("# none\n")
    return {
        "search": search,
        "empty_items": empty_items,
        "routes": [
            ("*api.github.com/repos/acme/fromawesome*", awesome_meta),
            ("*api.github.com/repos/acme/fromhn*", hn_meta),
            ("*hn.algolia.com*self-hosted*", hn_hits),
            ("*hn.algolia.com*", empty_hits),
            ("*e2b-dev/awesome-ai-agents*", awesome_md),
            ("*raw.githubusercontent.com*", empty_md),
        ],
    }


class _Call:
    def __init__(self, ts: float, n: str, auth: str, url: str) -> None:
        self.ts = ts
        self.n = n
        self.auth = auth
        self.url = url


def _calls(log: Path) -> list[_Call]:
    out = []
    for line in log.read_text().splitlines():
        ts, n, auth, url = line.split("\t", 3)
        out.append(_Call(float(ts), n, auth, url))
    return out


def _discover(memory: Path) -> dict:
    return json.loads((memory / "claw-ecosystem-discoveries.json").read_text())


def _assert_other_sources_kept(memory: Path, calls: list[_Call]) -> None:
    payload = _discover(memory)
    assert payload["mode"] == "discover"
    found = {row["repo"]: row for row in payload["newDiscoveries"]}
    assert found["acme/fromsearch"]["source"] == "github-search"
    assert found["acme/fromawesome"]["source"] == "awesome-list"
    assert found["acme/fromhn"]["source"] == "hackernews"
    sources = json.loads((memory / "claw-ecosystem-sources.json").read_text())
    curated = [row["repo"] for row in sources["curated"]]
    dynamic = [row["repo"] for row in sources["dynamic"]]
    assert curated == ["acme/already"]
    assert "acme/old" in dynamic
    assert "acme/fromsearch" in dynamic
    urls = [call.url for call in calls]
    assert all("telegram" not in url for url in urls)
    assert all("api.telegram.org" not in url for url in urls)


def test_search_sends_token_when_set(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    _init_memory(memory)
    fix = _fixtures(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    _write_curl(
        bindir, log, tmp_path / "search.count", fix["routes"],
        search_mode="ok", ok_search=fix["search"], empty_items=fix["empty_items"],
    )
    proc = _run(memory, _env(memory, bindir, GITHUB_TOKEN=TOKEN))
    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    search = [c for c in calls if "search/repositories" in c.url]
    assert len(search) == 9
    github = [c for c in calls if "api.github.com" in c.url]
    assert github
    assert all(c.auth == f"Authorization: Bearer {TOKEN}" for c in github)
    assert TOKEN not in (memory / "claw-ecosystem-discoveries.json").read_text()
    assert "skipping remaining search queries" not in proc.stderr
    _assert_other_sources_kept(memory, calls)
    assert "telegram" not in proc.stdout
    assert "api.telegram.org" not in proc.stdout
    assert "api.telegram.org" not in proc.stderr


def test_search_runs_without_token(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    _init_memory(memory)
    fix = _fixtures(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    _write_curl(
        bindir, log, tmp_path / "search.count", fix["routes"],
        search_mode="ok", ok_search=fix["search"], empty_items=fix["empty_items"],
    )
    env = _env(memory, bindir)
    env.pop("GITHUB_TOKEN", None)
    proc = _run(memory, env)
    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    search = [c for c in calls if "search/repositories" in c.url]
    assert len(search) == 9
    assert all(c.auth == "" for c in calls)
    assert "Authorization" not in log.read_text()
    _assert_other_sources_kept(memory, calls)
    assert "api.telegram.org" not in proc.stdout
    assert "api.telegram.org" not in proc.stderr


def test_search_403_backs_off_then_skips_and_keeps_other_sources(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    _init_memory(memory)
    fix = _fixtures(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    _write_curl(
        bindir, log, tmp_path / "search.count", fix["routes"],
        search_mode="block_after_first",
        ok_search=fix["search"],
        empty_items=fix["empty_items"],
        retry_after="3",
        reset_at="9999999999",
    )
    proc = _run(memory, _env(memory, bindir, GITHUB_TOKEN=TOKEN))
    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    search = [c for c in calls if "search/repositories" in c.url]
    assert [c.n for c in search] == ["1", "2", "3"]
    # Retry-After is 3s. The reset header is centuries out; a wait that long,
    # or the 20s cap, means we ignored Retry-After.
    delta = search[2].ts - search[1].ts
    assert 2.5 <= delta <= 8, delta
    assert proc.stderr.count("skipping remaining search queries") == 1
    assert "GitHub search blocked (HTTP 403)" in proc.stderr
    assert "HTTP Error 403" not in proc.stderr
    _assert_other_sources_kept(memory, calls)
    assert "api.telegram.org" not in proc.stdout
    assert "api.telegram.org" not in proc.stderr
    state = json.loads((memory / "claw-ecosystem-state.json").read_text())
    assert state["lastDiscovery"]
    assert state["lastSeenReleases"] == {}


def test_search_403_retry_keeps_going_when_the_retry_works(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    _init_memory(memory)
    fix = _fixtures(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    _write_curl(
        bindir, log, tmp_path / "search.count", fix["routes"],
        search_mode="retry_then_ok",
        ok_search=fix["search"],
        empty_items=fix["empty_items"],
        retry_after="0",
        reset_at="9999999999",
    )
    proc = _run(memory, _env(memory, bindir, GITHUB_TOKEN=TOKEN))
    assert proc.returncode == 0, proc.stderr
    calls = _calls(log)
    search = [c for c in calls if "search/repositories" in c.url]
    # Nine queries, plus one retry of the first. Retry-After 0 must not sleep
    # on the far-future X-RateLimit-Reset.
    assert len(search) == 10
    assert search[0].ts <= search[1].ts
    assert search[1].ts - search[0].ts < 1.5
    assert "skipping remaining search queries" not in proc.stderr
    _assert_other_sources_kept(memory, calls)
    assert "api.telegram.org" not in proc.stdout
    assert "api.telegram.org" not in proc.stderr
