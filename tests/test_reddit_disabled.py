"""Reddit fetch is off until OAuth exists. The fetcher code stays."""
import importlib.util
from pathlib import Path

import source_health as sh

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fetch_constant_is_off():
    text = (SCRIPTS / "claw-reddit-monitor.py").read_text()
    assert "REDDIT_FETCH_ENABLED = False" in text
    assert sh.reddit_fetch_enabled() is False


def test_reddit_monitor_does_not_fetch_or_save_when_disabled(monkeypatch):
    mod = sh._reddit_monitor()
    assert mod.REDDIT_FETCH_ENABLED is False

    def _boom(*args, **kwargs):
        raise AssertionError(f"unexpected fetch: {args!r}")

    monkeypatch.setattr(mod, "urlopen", _boom)
    monkeypatch.setattr(mod, "save_state", lambda state: (_ for _ in ()).throw(AssertionError("save")))
    monkeypatch.setattr(mod, "load_state", lambda: (_ for _ in ()).throw(AssertionError("load")))
    assert mod.check_subreddits(verbose=False) == ([], {})


def test_legacy_daily_collect_does_not_invoke_reddit():
    daily = (SCRIPTS.parent / "clawbytes_daily.py").read_text()
    assert "python3 scripts/claw-reddit-monitor.py" not in daily
