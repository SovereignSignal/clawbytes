"""claw-discover.py search sends GITHUB_TOKEN and stops after one blocked retry."""

import importlib.util
import json
from http.client import HTTPMessage
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claw-discover.py"


def _load():
    spec = importlib.util.spec_from_file_location("claw_discover_search", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _headers(**pairs: str) -> HTTPMessage:
    msg = HTTPMessage()
    for key, value in pairs.items():
        msg[key] = value
    return msg


def _item(repo: str, stars: int = 500) -> dict:
    name = repo.split("/", 1)[1]
    return {
        "full_name": repo,
        "name": name,
        "description": "agent",
        "stargazers_count": stars,
        "html_url": f"https://github.com/{repo}",
        "topics": [],
        "language": "Python",
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


def _http_error(url: str, code: int, headers: HTTPMessage) -> HTTPError:
    return HTTPError(url, code, "Blocked", headers, None)


def test_retry_after_zero_does_not_use_reset_and_wait_is_capped():
    mod = _load()
    assert mod.retry_wait_seconds(_headers(**{
        "Retry-After": "3",
        "X-RateLimit-Reset": "9999999999",
    })) == 3
    assert mod.retry_wait_seconds(_headers(**{
        "Retry-After": "0",
        "X-RateLimit-Reset": "9999999999",
    })) == 0
    assert mod.bound_search_wait(9999999999) == mod.SEARCH_BACKOFF_CAP
    assert mod.bound_search_wait(0) == 0


def test_search_sends_token_and_runs_without_one(monkeypatch):
    mod = _load()
    mod.GITHUB_QUERIES = ["one", "two"]
    calls = []

    def urlopen(req, timeout=15):
        calls.append(req)
        return _Resp({"items": [_item("acme/one")] if "q=one" in req.full_url else []})

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    monkeypatch.setenv("GITHUB_TOKEN", "test-token-xyz")
    found = mod.discover_github(set())
    assert [row["repo"] for row in found] == ["acme/one"]
    assert all(req.get_header("Authorization") == "Bearer test-token-xyz" for req in calls)
    assert all("api.telegram.org" not in req.full_url for req in calls)

    calls.clear()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    found = mod.discover_github(set())
    assert [row["repo"] for row in found] == ["acme/one"]
    assert calls
    assert all(req.get_header("Authorization") is None for req in calls)
    assert all("api.telegram.org" not in req.full_url for req in calls)


def test_search_403_backs_off_once_then_skips_and_keeps_earlier_results(monkeypatch, capsys):
    mod = _load()
    mod.GITHUB_QUERIES = ["one", "two", "three"]
    calls = []
    slept = []
    blocked = _headers(**{"Retry-After": "3", "X-RateLimit-Reset": "9999999999"})

    def urlopen(req, timeout=15):
        calls.append(req.full_url)
        if "q=one" in req.full_url:
            return _Resp({"items": [_item("acme/kept")]})
        if calls.count(req.full_url) <= 2 and "q=two" in req.full_url:
            raise _http_error(req.full_url, 403, blocked)
        return _Resp({"items": [_item("acme/should-not-appear")]})

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-xyz")

    found = mod.discover_github(set())
    err = capsys.readouterr().err
    assert [row["repo"] for row in found] == ["acme/kept"]
    assert slept == [3]
    assert err.count("skipping remaining search queries") == 1
    assert "GitHub search blocked (HTTP 403)" in err
    assert "HTTP Error 403" not in err
    assert not any("q=three" in url for url in calls)
    assert all("api.telegram.org" not in url for url in calls)
    assert sum("q=two" in url for url in calls) == 2


def test_search_429_uses_the_same_single_skip(monkeypatch, capsys):
    mod = _load()
    mod.GITHUB_QUERIES = ["one", "two"]
    calls = []
    slept = []
    blocked = _headers(**{"Retry-After": "1", "X-RateLimit-Reset": "9999999999"})

    def urlopen(req, timeout=15):
        calls.append(req.full_url)
        raise _http_error(req.full_url, 429, blocked)

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    found = mod.discover_github(set())
    err = capsys.readouterr().err
    assert found == []
    assert slept == [1]
    assert err.count("skipping remaining search queries") == 1
    assert "GitHub search blocked (HTTP 429)" in err
    assert not any("q=two" in url for url in calls)
    assert all("api.telegram.org" not in url for url in calls)
