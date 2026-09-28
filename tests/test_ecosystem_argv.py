"""Large JSON must not reach jq through argv or an exported variable.

Linux rejects execve when any argument or environment string exceeds
MAX_ARG_STRLEN (128 KiB). Bash reports that as "jq: Argument list too long".
The discover append sits inside a command substitution, so the old script
kept going and finished with a partial list. These runs force both shapes:
one repo document over 128 KiB, and an accumulated discovery array over
128 KiB, with allexport on and the accumulator names already exported.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claw-ecosystem-monitor.sh"
HUGE = 180_000
MEDIUM = 4_000
MEDIUM_COUNT = 40


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def _write_curl(bindir: Path, log: Path, routes: list[tuple[str, Path]]) -> None:
    """routes: (case glob, file). First match wins. Anything else is `{}`."""
    arms = [f"  {pattern}) cat {_quote(path)} ;;" for pattern, path in routes]
    script = "\n".join([
        "#!/bin/sh",
        f"log={_quote(log)}",
        'url=""',
        'for arg in "$@"; do',
        '  case "$arg" in',
        '    http://*|https://*) url="$arg" ;;',
        "  esac",
        "done",
        'printf "%s\\n" "$url" >> "$log"',
        'case "$url" in',
        *arms,
        "  *) printf '%s\\n' '{}' ;;",
        "esac",
        "",
    ])
    curl = bindir / "curl"
    curl.write_text(script)
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)


def _env(memory: Path, bindir: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "CLAWBYTES_MEMORY_DIR": str(memory),
        "WORKSPACE": str(memory.parent),
        "GITHUB_TOKEN": "",
        # Inherit allexport, and pre-export the names the script assigns.
        # Without the unexport/set +a fix, growing those values trips
        # MAX_ARG_STRLEN even when jq's own arguments are small.
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


def _run(memory: Path, env: dict[str, str], mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--mode", mode],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_memory(memory: Path, sources: dict, state: dict) -> None:
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "claw-ecosystem-sources.json").write_text(json.dumps(sources))
    (memory / "claw-ecosystem-state.json").write_text(json.dumps(state))


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


def test_discover_accepts_json_past_argv_limit(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    _init_memory(
        memory,
        {"curated": [], "dynamic": [], "_meta": {}},
        {
            "lastCheck": None,
            "lastDiscovery": None,
            "lastSeenReleases": {},
            "lastSeenHNStories": [],
            "lastSeenSkills": [],
        },
    )
    items = [_gh_item("acme/huge", "H" * HUGE)]
    items.extend(_gh_item(f"acme/med{i:02d}", "m" * MEDIUM) for i in range(MEDIUM_COUNT))
    search = tmp_path / "search.json"
    search.write_text(json.dumps({"items": items}))
    awesome_meta = tmp_path / "awesome-meta.json"
    awesome_meta.write_text(json.dumps(_repo_api("acme/fromawesome", "listed", 200)))
    hn_meta = tmp_path / "hn-meta.json"
    hn_meta.write_text(json.dumps(_repo_api("acme/fromhn", "discussed", 80)))
    awesome_md = tmp_path / "awesome.md"
    awesome_md.write_text("# Agents\n- https://github.com/acme/fromawesome\n")
    hn_hits = tmp_path / "hn.json"
    hn_hits.write_text(json.dumps({"hits": [{"url": "https://github.com/acme/fromhn"}]}))
    empty_items = tmp_path / "empty-items.json"
    empty_items.write_text('{"items":[]}\n')
    empty_hits = tmp_path / "empty-hits.json"
    empty_hits.write_text('{"hits":[]}\n')
    empty_md = tmp_path / "empty.md"
    empty_md.write_text("# none\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    _write_curl(bindir, log, [
        ("*search/repositories*topic:ai-agent+topic:openclaw*", search),
        ("*search/repositories*", empty_items),
        ("*api.github.com/repos/acme/fromawesome*", awesome_meta),
        ("*api.github.com/repos/acme/fromhn*", hn_meta),
        ("*hn.algolia.com*self-hosted*", hn_hits),
        ("*hn.algolia.com*", empty_hits),
        ("*e2b-dev/awesome-ai-agents*", awesome_md),
        ("*raw.githubusercontent.com*", empty_md),
    ])

    proc = _run(memory, _env(memory, bindir), "discover")
    assert proc.returncode == 0, proc.stderr
    assert "Argument list too long" not in proc.stderr
    urls = log.read_text().splitlines()
    assert urls
    assert all("telegram" not in url for url in urls)

    payload = json.loads((memory / "claw-ecosystem-discoveries.json").read_text())
    assert payload["mode"] == "discover"
    found = {row["repo"]: row for row in payload["newDiscoveries"]}
    assert payload["summary"]["discoveryCount"] == len(found) == MEDIUM_COUNT + 3
    assert payload["summary"]["hasDiscoveries"] is True
    assert len(found["acme/huge"]["description"]) == HUGE
    assert set(found["acme/huge"]["description"]) == {"H"}
    assert found["acme/huge"]["source"] == "github-search"
    assert found["acme/huge"]["isNew"] is True
    assert len(found["acme/med00"]["description"]) == MEDIUM
    assert found["acme/med39"]["source"] == "github-search"
    assert found["acme/fromawesome"]["source"] == "awesome-list"
    assert found["acme/fromhn"]["source"] == "hackernews"
    assert (memory / "claw-ecosystem-discoveries.json").stat().st_size > 128 * 1024

    sources = json.loads((memory / "claw-ecosystem-sources.json").read_text())
    dynamic = {row["repo"]: row for row in sources["dynamic"]}
    assert set(dynamic) == set(found)
    assert all(row.get("discoveredAt") for row in dynamic.values())
    assert sources["_meta"]["totalDiscovered"] == len(dynamic)


def test_check_accepts_release_name_past_argv_limit(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    _init_memory(
        memory,
        {"curated": [{"repo": "acme/widget"}], "dynamic": [], "_meta": {}},
        {
            "lastCheck": None,
            "lastSeenReleases": {},
            "lastSeenHNStories": [],
            "lastSeenSkills": [],
        },
    )
    body = tmp_path / "release.json"
    body.write_text(json.dumps([{
        "tag_name": "v1.0.0",
        "name": "v1.0.0",
        "html_url": "https://github.com/acme/widget/releases/tag/v1.0.0",
        "published_at": "2026-09-23T00:00:00Z",
        "body": "first",
    }]))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    _write_curl(bindir, log, [
        ("*api.github.com/repos/acme/widget/releases*", body),
    ])
    env = _env(memory, bindir, CLAWBYTES_ECOSYSTEM_RELEASES_ONLY="1")

    first = _run(memory, env, "check")
    assert first.returncode == 0, first.stderr
    assert "Argument list too long" not in first.stderr
    opened = json.loads((memory / "claw-ecosystem-new-items.json").read_text())
    assert opened["newReleases"] == []
    assert opened["summary"]["hasNews"] is False
    state = json.loads((memory / "claw-ecosystem-state.json").read_text())
    assert state["lastSeenReleases"]["acme/widget"] == "v1.0.0"

    body.write_text(json.dumps([{
        "tag_name": "v2.0.0",
        "name": "N" * HUGE,
        "html_url": "https://github.com/acme/widget/releases/tag/v2.0.0",
        "published_at": "2026-09-28T00:00:00Z",
        "body": "second",
    }]))
    second = _run(memory, env, "check")
    assert second.returncode == 0, second.stderr
    assert "Argument list too long" not in second.stderr
    urls = log.read_text().splitlines()
    assert urls
    assert all("telegram" not in url for url in urls)

    posted = json.loads((memory / "claw-ecosystem-new-items.json").read_text())
    assert posted["mode"] == "check"
    assert posted["summary"]["releaseCount"] == 1
    assert posted["summary"]["hasNews"] is True
    assert len(posted["newReleases"]) == 1
    release = posted["newReleases"][0]
    assert release["tag"] == "v2.0.0"
    assert release["repo"] == "acme/widget"
    assert len(release["name"]) == HUGE
    assert set(release["name"]) == {"N"}
    assert (memory / "claw-ecosystem-new-items.json").stat().st_size > 128 * 1024
    state = json.loads((memory / "claw-ecosystem-state.json").read_text())
    # The tag stays unseen until collect marks it. A huge name must not
    # disturb that, and must not be written into lastSeenReleases.
    assert state["lastSeenReleases"]["acme/widget"] == "v1.0.0"
    assert "N" * 20 not in json.dumps(state["lastSeenReleases"])
