"""Curator and writer model fallbacks, plus the writer number guard.

Primary model failures (timeout, HTTP error, empty content, unparseable
curator JSON, or a writer post that introduces a number) try the fallback
model once. Empty `content` is a failure even when a reasoning field is set.
"""

import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("claw_curator_fallback", SCRIPTS / "curator.py")
curator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(curator)

PRIMARY_CURATOR = "deepseek-v4-pro"
FALLBACK_CURATOR = "deepseek-v4.1-flash"
PRIMARY_WRITER = "gemma-primary"
FALLBACK_WRITER = "glm-5.3-flash"

ITEM = {
    "title": "Aider 0.86.0",
    "url": "",
    "summary": "adds diff mode on 2026-04-01",
    "sourceType": "rss",
}

GROUNDED = (
    "📚 <b>Read</b> — 1 item\n\n"
    "📚 <a href=\"\">Aider 0.86.0</a> — adds diff mode on 2026-04-01"
)

INVENTED = (
    "📚 <b>Read</b> — 1 item\n\n"
    "📚 <a href=\"\">Aider 0.86.0</a> — adds diff mode on 2026-04-01, capped at 150"
)


class _Resp:
    def __init__(self, raw: bytes):
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _completion(content, reasoning=None, reasoning_content=None) -> bytes:
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return json.dumps({
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }).encode()


def _req_json(req) -> dict:
    return json.loads(req.data.decode())


def _enable_curator(monkeypatch, fallback=None):
    monkeypatch.setenv("CLAWBYTES_CURATOR_URL", "https://ollama.example/v1")
    monkeypatch.setenv("CLAWBYTES_CURATOR_MODEL", PRIMARY_CURATOR)
    monkeypatch.setenv("CLAWBYTES_CURATOR_API_KEY", "test-key")
    if fallback is None:
        monkeypatch.delenv("CLAWBYTES_CURATOR_MODEL_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("CLAWBYTES_CURATOR_MODEL_FALLBACK", fallback)


def _patch_urllib(monkeypatch, handler):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", handler)


def _curated_json(title: str) -> str:
    return json.dumps({"lane": "read", "items": [{"id": "1", "title": title}]})


def _route_curator(monkeypatch, primary_result):
    """Primary returns primary_result (bytes or exception); fallback returns JSON."""
    calls = []

    def urlopen(req, timeout=0):
        body = _req_json(req)
        calls.append(body["model"])
        if body["model"] == PRIMARY_CURATOR:
            if isinstance(primary_result, Exception):
                raise primary_result
            return _Resp(primary_result)
        return _Resp(_completion(_curated_json("from-fallback")))

    _patch_urllib(monkeypatch, urlopen)
    return calls


def _enable_writer(monkeypatch, fallback=None):
    monkeypatch.setattr(ct, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(ct, "LLM_URL", "https://llm.example/v1")
    monkeypatch.setattr(ct, "LLM_MODEL", PRIMARY_WRITER)
    if fallback is None:
        monkeypatch.delenv("CLAWBYTES_LLM_MODEL_FALLBACK", raising=False)
    else:
        monkeypatch.setenv("CLAWBYTES_LLM_MODEL_FALLBACK", fallback)


def _route_writer(monkeypatch, primary_result, fallback_text=GROUNDED):
    calls = []
    prompts = []
    temperatures = []

    def urlopen(req, timeout=45):
        body = _req_json(req)
        calls.append(body["model"])
        prompts.append(body["messages"][0]["content"])
        temperatures.append(body["temperature"])
        if body["model"] == PRIMARY_WRITER:
            if isinstance(primary_result, Exception):
                raise primary_result
            return _Resp(primary_result)
        return _Resp(_completion(fallback_text))

    monkeypatch.setattr(ct, "urlopen", urlopen)
    return calls, prompts, temperatures


def test_numbers_grounded_requires_verbatim_versions_and_dates():
    source = "Aider 0.86.0 on 2026-04-01\n1"
    assert ct.numbers_grounded("ships 0.86.0", source)
    assert ct.numbers_grounded("no numbers here", source)
    assert not ct.numbers_grounded("ships 0.87.0", source)
    assert not ct.numbers_grounded("ships 0.86", source)
    assert not ct.numbers_grounded("released 2026-04-02", source)
    assert not ct.numbers_grounded("moved to 2026-01-04", "released 2026-04-01")
    assert not ct.numbers_grounded("on 04/01/2026", "released 2026-04-01")
    dated = "v2 released 2024-01-15"
    assert ct.numbers_grounded("v2 on 2024-01-15", dated)
    assert not ct.numbers_grounded("v2 date 2024-2-15", dated)
    assert ct.numbers_grounded("about 1,200 stars", "repo has 1,200 stars")
    assert not ct.numbers_grounded("about 200 stars", "repo has 1,200 stars")
    assert not ct.numbers_grounded("about 1200 stars", "repo has 1,200 stars")


def test_curator_timeout_retries_default_fallback_once(monkeypatch, capsys):
    _enable_curator(monkeypatch)
    calls = _route_curator(monkeypatch, TimeoutError("timed out"))
    out = curator.curate({"lane": "read", "items": [{"id": "1", "title": "primary"}]})
    assert calls == [PRIMARY_CURATOR, FALLBACK_CURATOR]
    assert out["items"][0]["title"] == "from-fallback"
    assert out["_curator"]["fallback"] is False
    assert out["_curator"]["model"] == FALLBACK_CURATOR
    assert f"[curator] answered by model={FALLBACK_CURATOR}" in capsys.readouterr().err


def test_curator_http_error_retries_fallback(monkeypatch):
    _enable_curator(monkeypatch)
    err = HTTPError("https://ollama.example/v1/chat/completions", 503, "unavailable", {}, None)
    calls = _route_curator(monkeypatch, err)
    out = curator.curate({"lane": "read", "items": [{"id": "1"}]})
    assert calls == [PRIMARY_CURATOR, FALLBACK_CURATOR]
    assert out["items"][0]["title"] == "from-fallback"
    assert out["_curator"]["fallback"] is False


def test_curator_empty_content_ignores_reasoning_and_retries(monkeypatch):
    _enable_curator(monkeypatch)
    primary = _completion(
        "",
        reasoning=_curated_json("from-reasoning"),
        reasoning_content=_curated_json("from-reasoning-content"),
    )
    calls = _route_curator(monkeypatch, primary)
    out = curator.curate({"lane": "read", "items": [{"id": "1", "title": "kept"}]})
    assert calls == [PRIMARY_CURATOR, FALLBACK_CURATOR]
    assert out["items"][0]["title"] == "from-fallback"
    assert "from-reasoning" not in json.dumps(out)


def test_curator_unparseable_json_retries_fallback(monkeypatch):
    _enable_curator(monkeypatch)
    calls = _route_curator(monkeypatch, _completion("sure, here is the bundle: not-json"))
    out = curator.curate({"lane": "read", "items": [{"id": "1"}]})
    assert calls == [PRIMARY_CURATOR, FALLBACK_CURATOR]
    assert out["items"][0]["title"] == "from-fallback"
    assert out["_curator"]["fallback"] is False


def test_curator_primary_success_does_not_call_fallback(monkeypatch, capsys):
    _enable_curator(monkeypatch)
    calls = _route_curator(monkeypatch, _completion(_curated_json("from-primary")))
    out = curator.curate({"lane": "read", "items": [{"id": "1"}]})
    assert calls == [PRIMARY_CURATOR]
    assert out["items"][0]["title"] == "from-primary"
    assert out["_curator"]["model"] == PRIMARY_CURATOR
    assert f"[curator] answered by model={PRIMARY_CURATOR}" in capsys.readouterr().err


def test_curator_both_models_fail_keeps_posting_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWBYTES_MEMORY_DIR", str(tmp_path))
    _enable_curator(monkeypatch)
    calls = []

    def urlopen(req, timeout=0):
        body = _req_json(req)
        calls.append(body["model"])
        if body["model"] == PRIMARY_CURATOR:
            raise URLError(TimeoutError("timed out"))
        return _Resp(_completion("   ", reasoning=_curated_json("from-reasoning")))

    _patch_urllib(monkeypatch, urlopen)
    bundle = {"lane": "read", "items": [{"id": "1", "title": "original"}]}
    out = curator.curate(bundle)
    assert calls == [PRIMARY_CURATOR, FALLBACK_CURATOR]
    assert out["items"] == bundle["items"]
    assert out["_curator"]["approved"] is True
    assert out["_curator"]["fallback"] is True
    assert (tmp_path / "degraded_publishes.json").exists()


def test_curator_fallback_success_does_not_log_degraded(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWBYTES_MEMORY_DIR", str(tmp_path))
    _enable_curator(monkeypatch)
    _route_curator(monkeypatch, TimeoutError("timed out"))
    curator.curate({"lane": "read", "items": [{"id": "1"}]})
    assert not (tmp_path / "degraded_publishes.json").exists()


def test_curator_empty_fallback_env_skips_second_call(monkeypatch):
    _enable_curator(monkeypatch, fallback="")
    calls = _route_curator(monkeypatch, TimeoutError("timed out"))
    out = curator.curate({"lane": "read", "items": [{"id": "1", "title": "original"}]})
    assert calls == [PRIMARY_CURATOR]
    assert out["_curator"]["fallback"] is True
    assert out["items"][0]["title"] == "original"


def test_writer_timeout_retries_default_fallback_once(monkeypatch, capsys):
    _enable_writer(monkeypatch)
    calls, prompts, temperatures = _route_writer(monkeypatch, TimeoutError("timed out"))
    out = ct.llm_summarize([ITEM], "read")
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]
    assert out == GROUNDED
    assert temperatures == [0.2, 0.2]
    prompt = prompts[0]
    assert "only facts present" in prompt
    assert "verbatim" in prompt
    assert 'Do not write "launches" or "opens" unless the source text says so.' in prompt
    assert "massive" in prompt
    assert "game-changing" in prompt
    assert "one of the largest" in prompt
    assert f"llm_summarize(read): answered by model={FALLBACK_WRITER}" in capsys.readouterr().err


def test_writer_empty_content_ignores_reasoning(monkeypatch):
    _enable_writer(monkeypatch)
    primary = _completion(
        None,
        reasoning=GROUNDED + " REASONING ONLY",
        reasoning_content=GROUNDED + " REASONING CONTENT",
    )
    calls, _, _ = _route_writer(monkeypatch, primary)
    out = ct.llm_summarize([ITEM], "read")
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]
    assert out == GROUNDED
    assert "REASONING" not in out


def test_writer_http_error_retries_fallback(monkeypatch):
    _enable_writer(monkeypatch)
    err = HTTPError("https://llm.example/v1/chat/completions", 500, "boom", {}, None)
    calls, _, temperatures = _route_writer(monkeypatch, err)
    out = ct.llm_summarize([ITEM], "read")
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]
    assert out == GROUNDED
    assert max(temperatures) <= 0.2


def test_writer_number_guard_retries_fallback_then_template(monkeypatch):
    _enable_writer(monkeypatch)
    calls, _, _ = _route_writer(monkeypatch, _completion(INVENTED), fallback_text=INVENTED)
    assert ct.llm_summarize([ITEM], "read") is None
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]

    calls.clear()
    monkeypatch.setattr(ct, "bundle_for_category", lambda *a, **k: [ITEM])
    rendered = ct.format_category_bundle("read")
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]
    assert "150" not in rendered
    assert "Aider 0.86.0" in rendered
    assert "adds diff mode on 2026-04-01" in rendered


def test_writer_number_guard_accepts_fallback_when_grounded(monkeypatch, capsys):
    _enable_writer(monkeypatch)
    calls, _, _ = _route_writer(monkeypatch, _completion(INVENTED), fallback_text=GROUNDED)
    out = ct.llm_summarize([ITEM], "read")
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]
    assert out == GROUNDED
    err = capsys.readouterr().err
    assert "number guard rejected model=gemma-primary" in err
    assert f"answered by model={FALLBACK_WRITER}" in err


def test_writer_grounded_primary_does_not_call_fallback(monkeypatch):
    _enable_writer(monkeypatch)
    calls, _, temperatures = _route_writer(monkeypatch, _completion(GROUNDED))
    out = ct.llm_summarize([ITEM], "read")
    assert calls == [PRIMARY_WRITER]
    assert out == GROUNDED
    assert temperatures == [0.2]


def test_writer_count_is_allowed_index_numbers_are_not_a_license(monkeypatch):
    """The header count is a fact. Item indexes in the prompt are not."""
    items = [
        {"title": "Alpha release", "url": "", "summary": "adds hooks", "sourceType": "rss"},
        {"title": "Gamma notes", "url": "", "summary": "fixes auth", "sourceType": "rss"},
    ]
    counted = (
        "📚 <b>Read</b> — 2 items\n\n"
        "📚 <a href=\"\">Alpha release</a> — adds hooks\n"
        "📚 <a href=\"\">Gamma notes</a> — fixes auth for operators"
    )
    invented_index = (
        "📚 <b>Read</b> — 2 items\n\n"
        "📚 <a href=\"\">Alpha release</a> — adds hooks across 1 repo\n"
        "📚 <a href=\"\">Gamma notes</a> — fixes auth for operators"
    )
    _enable_writer(monkeypatch)
    calls, _, _ = _route_writer(monkeypatch, _completion(counted))
    assert ct.llm_summarize(items, "read") == counted
    assert calls == [PRIMARY_WRITER]

    calls.clear()
    calls2, _, _ = _route_writer(monkeypatch, _completion(invented_index), fallback_text=counted)
    assert ct.llm_summarize(items, "read") == counted
    assert calls2 == [PRIMARY_WRITER, FALLBACK_WRITER]


def test_writer_empty_fallback_env_skips_second_call(monkeypatch):
    _enable_writer(monkeypatch, fallback="")
    calls, _, _ = _route_writer(monkeypatch, TimeoutError("timed out"))
    assert ct.llm_summarize([ITEM], "read") is None
    assert calls == [PRIMARY_WRITER]


def test_writer_custom_fallback_model_is_used(monkeypatch):
    _enable_writer(monkeypatch, fallback="backup-writer")
    calls, _, _ = _route_writer(monkeypatch, TimeoutError("timed out"), fallback_text=GROUNDED)
    out = ct.llm_summarize([ITEM], "read")
    assert out == GROUNDED
    assert calls == [PRIMARY_WRITER, "backup-writer"]


def test_writer_refusal_and_short_draft_try_fallback(monkeypatch):
    refusal = "I cannot write this lane from the item text without adding claims."
    _enable_writer(monkeypatch)
    calls, _, _ = _route_writer(monkeypatch, _completion(refusal))
    assert ct.llm_summarize([ITEM], "read") == GROUNDED
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]

    calls.clear()
    calls2, _, _ = _route_writer(monkeypatch, _completion("too short"), fallback_text=GROUNDED)
    assert ct.llm_summarize([ITEM], "read") == GROUNDED
    assert calls2 == [PRIMARY_WRITER, FALLBACK_WRITER]

    calls2.clear()
    calls3, _, _ = _route_writer(monkeypatch, _completion(refusal), fallback_text=refusal)
    assert ct.llm_summarize([ITEM], "read") is None
    assert calls3 == [PRIMARY_WRITER, FALLBACK_WRITER]


def test_writer_blank_content_tries_fallback(monkeypatch):
    _enable_writer(monkeypatch)
    calls, _, _ = _route_writer(monkeypatch, _completion("   "))
    assert ct.llm_summarize([ITEM], "read") == GROUNDED
    assert calls == [PRIMARY_WRITER, FALLBACK_WRITER]


def test_writer_same_fallback_name_does_not_double_call(monkeypatch):
    _enable_writer(monkeypatch, fallback=PRIMARY_WRITER)
    calls, _, _ = _route_writer(monkeypatch, TimeoutError("timed out"))
    assert ct.llm_summarize([ITEM], "read") is None
    assert calls == [PRIMARY_WRITER]


def test_curator_custom_fallback_model(monkeypatch):
    _enable_curator(monkeypatch, fallback="backup-curator")
    calls = _route_curator(monkeypatch, TimeoutError("timed out"))
    out = curator.curate({"lane": "read", "items": [{"id": "1"}]})
    assert calls == [PRIMARY_CURATOR, "backup-curator"]
    assert out["items"][0]["title"] == "from-fallback"
    assert out["_curator"]["model"] == "backup-curator"


def test_curator_same_fallback_name_does_not_double_call(monkeypatch):
    _enable_curator(monkeypatch, fallback=PRIMARY_CURATOR)
    calls = _route_curator(monkeypatch, TimeoutError("timed out"))
    out = curator.curate({"lane": "read", "items": [{"id": "1", "title": "original"}]})
    assert calls == [PRIMARY_CURATOR]
    assert out["_curator"]["fallback"] is True


def test_curator_timeout_budget_reserves_a_fallback_slice():
    first = curator._curator_call_timeout(290, index=0, count=2, elapsed=0)
    second = curator._curator_call_timeout(290, index=1, count=2, elapsed=first)
    assert 100 <= first <= 160
    assert second >= 30
    assert first + second <= 290
    # A fast primary failure leaves the fallback most of the budget.
    assert curator._curator_call_timeout(290, index=1, count=2, elapsed=2) >= 200


def test_curator_requested_timeouts_fit_the_process_budget(monkeypatch):
    _enable_curator(monkeypatch)
    seen = []

    def urlopen(req, timeout=0):
        body = _req_json(req)
        seen.append((body["model"], timeout))
        if body["model"] == PRIMARY_CURATOR:
            raise TimeoutError("timed out")
        return _Resp(_completion(_curated_json("from-fallback")))

    _patch_urllib(monkeypatch, urlopen)
    out = curator.curate({"lane": "read", "items": [{"id": "1"}]}, timeout=180)
    assert [model for model, _ in seen] == [PRIMARY_CURATOR, FALLBACK_CURATOR]
    assert seen[0][1] < 180
    assert seen[1][1] > 30
    assert out["items"][0]["title"] == "from-fallback"
