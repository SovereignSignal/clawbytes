import importlib.util
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
    monkeypatch.setattr(ct, "format_curated_messages", lambda cur, c: ["m1", "m2"])
    n = {}
    monkeypatch.setattr(ct, "send_telegram_message_list", lambda msgs, *a, **k: n.setdefault("n", len(msgs)) or len(msgs))
    monkeypatch.setattr(ct, "mark_posted", lambda *a, **k: n.setdefault("marked", a))
    ok, count = ct._publish_lane("ship", send=True)
    assert ok is True and count == 2 and n["n"] == 2


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
