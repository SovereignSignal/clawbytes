import importlib.util
import json
from pathlib import Path

import clawbytes_threads as ct

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("claw_curator", SCRIPTS / "curator.py")
curator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(curator)


def test_curator_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    assert ct._curator_enabled_for("ship") is False


def test_curator_lane_restriction(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    monkeypatch.setenv("CLAWBYTES_CURATOR_LANES", "ship,watch")
    assert ct._curator_enabled_for("ship") is True
    assert ct._curator_enabled_for("watch") is True
    assert ct._curator_enabled_for("read") is False
    assert ct._curator_enabled_for("community") is False


def test_publish_lane_deterministic_when_curator_off(monkeypatch):
    monkeypatch.delenv("CLAWBYTES_USE_CURATOR", raising=False)
    calls = {}
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "MSG")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "1"}, {"id": "2"}])
    monkeypatch.setattr(ct, "send_telegram", lambda m: calls.setdefault("tg", m))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: calls.setdefault("marked", True))
    # must NOT touch the curator path
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: (_ for _ in ()).throw(AssertionError("curator should not run")))
    sent, count = ct._publish_lane("ship", send=True)
    assert sent is True and count == 2 and calls["tg"] == "MSG" and calls["marked"]


def test_publish_lane_falls_back_when_curator_fails(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c, "items": []})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: None)  # curator failed
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "DET")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "1"}])
    sent = {}
    monkeypatch.setattr(ct, "send_telegram", lambda m: sent.setdefault("msg", m))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: None)
    ok, count = ct._publish_lane("ship", send=True)
    assert ok is True and count == 1 and sent["msg"] == "DET"  # deterministic fallback fired


def test_publish_lane_sends_curated_when_approved(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    curated = {"lane": "ship", "items": [{"id": "a"}, {"id": "b"}], "_curator": {"approved": True, "fallback": False}}
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: curated)
    monkeypatch.setattr(ct, "format_curated_html", lambda cur, c: "ONE CONSOLIDATED MESSAGE")
    n = {}
    # one consolidated send_telegram, NOT a per-item list
    monkeypatch.setattr(ct, "send_telegram", lambda m: n.setdefault("msg", m))
    monkeypatch.setattr(ct, "send_telegram_message_list", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should send one consolidated message")))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: n.setdefault("marked", a))
    ok, count = ct._publish_lane("ship", send=True)
    assert ok is True and count == 2 and n["msg"] == "ONE CONSOLIDATED MESSAGE"


def test_format_curated_html_is_consolidated_compact():
    curated = {
        "items": [
            {"title": "Aider 0.9", "url": "https://x/1", "blurb": "adds streaming"},
            {"title": "Cline 3.9", "url": "https://x/2", "blurb": "fixes MCP"},
        ],
        "take": "Two real shipments.",
    }
    out = ct.format_curated_html(curated, "ship")
    assert out.count("📦") == 2                      # one compact line per item
    assert "Ship — 2 items" in out                   # consolidated header w/ count
    assert "Aider 0.9</a> — adds streaming" in out   # emoji Title — blurb style
    assert "<i>Two real shipments.</i>" in out       # take at the end
    assert out.count("🚀") == 0


def test_publish_lane_empty_approved_items_fall_back(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    curated = {"lane": "ship", "items": [], "_curator": {"approved": True, "fallback": False}}
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: curated)
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "DET")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "1"}])
    sent = {}
    monkeypatch.setattr(ct, "send_telegram", lambda m: sent.setdefault("msg", m))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: None)
    ok, count = ct._publish_lane("ship", send=True)
    assert ok is True and count == 1 and sent["msg"] == "DET"


def test_publish_lane_gate_rejection_falls_back(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    curated = {"lane": "ship", "items": [{"id": "a", "title": "T", "url": "https://x"}], "_curator": {"approved": True, "fallback": False}}
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: curated)
    monkeypatch.setattr(ct, "format_curated_html", lambda cur, c: "<b>unbalanced")

    def _validate(message):
        if message == "DET":
            return (True, [])
        return (False, ["unbalanced"])

    monkeypatch.setattr(ct, "validate_lane_for_publish", _validate)
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "DET")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "1"}])
    sent = {}
    monkeypatch.setattr(ct, "send_telegram", lambda m: sent.setdefault("msg", m))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: None)
    ok, count = ct._publish_lane("ship", send=True)
    assert ok is True and count == 1 and sent["msg"] == "DET"


def test_format_curated_html_escapes_href():
    curated = {"items": [{"title": "A & B", "url": "https://x.test/a?b=1&c=2", "blurb": "ok"}]}
    out = ct.format_curated_html(curated, "ship")
    assert "b=1&amp;c=2" in out
    assert "A &amp; B" in out
    assert 'href="https://x.test/a?b=1&c=2"' not in out


def test_publish_lane_decline_logs_reason_and_still_publishes(monkeypatch, capsys):
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    long_notes = "n" * 400
    declined = {
        "lane": "community",
        "items": [{"id": "paper-1"}],
        "system_prompt": "SECRET PROMPT do not log sk-live-secret-token-value",
        "_curator": {
            "approved": False,
            "fallback": False,
            "skip_reason": "lane is arXiv and HF papers with no community signal",
            "notes": long_notes,
            "drop_reasons": {"paper-1": "research paper, not a community discussion"},
            "prompt": "full curator prompt should stay out of the log",
        },
    }
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: declined)
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "DET")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "paper-1"}])
    sent = {}
    monkeypatch.setattr(ct, "send_telegram", lambda m: sent.setdefault("msg", m))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: None)
    ok, count = ct._publish_lane("community", send=True)
    assert ok is True and count == 1 and sent["msg"] == "DET"
    err = capsys.readouterr().err
    assert "[autopublish] curator declined community:" in err
    assert "using deterministic bundle" in err
    assert "lane is arXiv and HF papers with no community signal" in err
    assert "research paper, not a community discussion" in err
    assert "SECRET PROMPT" not in err
    assert "sk-live-secret-token-value" not in err
    assert "full curator prompt" not in err
    # notes are included but truncated to ~300 chars of the reason
    assert long_notes not in err
    decline_line = next(line for line in err.splitlines() if "curator declined community:" in line)
    reason = decline_line.split("curator declined community:", 1)[1]
    reason = reason.split("; using deterministic bundle", 1)[0].strip()
    assert len(reason) <= 300


def test_publish_lane_curator_error_logs_and_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: None)
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "DET")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "1"}])
    sent = {}
    monkeypatch.setattr(ct, "send_telegram", lambda m: sent.setdefault("msg", m))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: None)
    ok, count = ct._publish_lane("community", send=True)
    assert ok is True and count == 1 and sent["msg"] == "DET"
    err = capsys.readouterr().err
    assert "[autopublish] curator error for community; using deterministic bundle" in err


def test_publish_lane_decline_falls_back_to_deterministic(monkeypatch):
    # Breadth over purity: a whole-lane decline must NOT silence the lane — it
    # falls back to the deterministic post. The curator's per-item drops still
    # apply on approved lanes; only a full decline triggers this.
    monkeypatch.setenv("CLAWBYTES_USE_CURATOR", "1")
    declined = {"lane": "read", "items": [{"id": "x"}], "_curator": {"approved": False, "fallback": False}}
    monkeypatch.setattr(ct, "curator_input_bundle", lambda c, *a, **k: {"lane": c})
    monkeypatch.setattr(ct, "run_curator_subprocess", lambda *a, **k: declined)
    monkeypatch.setattr(ct, "format_curated_messages", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send curated")))
    monkeypatch.setattr(ct, "format_category_bundle", lambda c, *a, **k: "DET")
    monkeypatch.setattr(ct, "bundle_for_category", lambda c, *a, **k: [{"id": "x"}])
    sent = {}
    monkeypatch.setattr(ct, "send_telegram", lambda m: sent.setdefault("msg", m))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: None)
    ok, count = ct._publish_lane("read", send=True)
    assert ok is True and count == 1 and sent["msg"] == "DET"


def test_ollama_curator_config_gate(monkeypatch):
    for v in ["CLAWBYTES_CURATOR_URL", "CLAWBYTES_CURATOR_MODEL", "CLAWBYTES_CURATOR_API_KEY", "CLAWBYTES_LLM_API_KEY"]:
        monkeypatch.delenv(v, raising=False)
    assert curator._ollama_curator_configured() is False
    monkeypatch.setenv("CLAWBYTES_CURATOR_URL", "https://ollama.com/v1")
    monkeypatch.setenv("CLAWBYTES_CURATOR_MODEL", "deepseek-v4-pro")
    assert curator._ollama_curator_configured() is False  # still no key
    monkeypatch.setenv("CLAWBYTES_LLM_API_KEY", "k")  # key can come from the shared LLM var
    assert curator._ollama_curator_configured() is True


def test_curator_memory_paths_follow_memory_dir(monkeypatch, tmp_path):
    """Curator state files follow CLAWBYTES_MEMORY_DIR, else repo memory/."""
    monkeypatch.setenv("CLAWBYTES_MEMORY_DIR", str(tmp_path))

    assert curator.memory_dir() == tmp_path
    assert curator.degraded_log_path() == tmp_path / "degraded_publishes.json"
    assert curator.discovered_refs_path() == tmp_path / "discovered_references.json"

    curator.log_degraded("ship", "timeout", "claude timed out")
    curator.persist_discovered_references([
        {"kind": "repo", "value": "https://github.com/example/tool", "why": "mentioned in a blurb"},
    ])

    degraded = json.loads((tmp_path / "degraded_publishes.json").read_text())
    assert degraded == [{
        "lane": "ship",
        "kind": "timeout",
        "message": "claude timed out",
        "at": degraded[0]["at"],
    }]
    assert isinstance(degraded[0]["at"], int)

    refs = json.loads((tmp_path / "discovered_references.json").read_text())
    assert refs[0]["kind"] == "repo"
    assert refs[0]["value"] == "https://github.com/example/tool"
    assert refs[0]["why"] == "mentioned in a blurb"
    assert isinstance(refs[0]["discovered_at"], int)
    assert list(refs[0]) == ["kind", "value", "why", "discovered_at"]

    monkeypatch.delenv("CLAWBYTES_MEMORY_DIR", raising=False)
    assert curator.memory_dir() == curator.REPO_ROOT / "memory"
    assert curator.degraded_log_path() == curator.REPO_ROOT / "memory" / "degraded_publishes.json"
    assert curator.discovered_refs_path() == curator.REPO_ROOT / "memory" / "discovered_references.json"
