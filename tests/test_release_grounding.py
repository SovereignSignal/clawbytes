import io
import json

import clawbytes_threads as ct


RAW_NOTES = """## What's Changed
* fix: bump deps by @dependabot in https://github.com/o/r/pull/101
* feat: add streaming tool calls by @dev in https://github.com/o/r/pull/102
* chore(deps): update lockfile by @dependabot[bot] in https://github.com/o/r/pull/103
* fix: PTY resize crash on linux by @dev in https://github.com/o/r/pull/104

## New Contributors
* @someone made their first contribution in https://github.com/o/r/pull/102

**Full Changelog**: https://github.com/o/r/compare/v1.0...v1.1"""


def test_clean_release_notes_keeps_features_drops_boilerplate():
    cleaned = ct.clean_release_notes(RAW_NOTES)
    assert "streaming tool calls" in cleaned
    assert "PTY resize crash" in cleaned
    assert "dependabot" not in cleaned
    assert "Full Changelog" not in cleaned
    assert "New Contributors" not in cleaned
    assert "https://github.com/o/r/pull" not in cleaned


def test_fetch_release_body_sends_auth_header_when_token_set(monkeypatch):
    captured = {}

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=0):
        captured["auth"] = req.get_header("Authorization")
        return FakeResp(json.dumps({"body": "feat: real notes"}).encode())

    monkeypatch.setenv("GITHUB_TOKEN", "test-token-123")
    monkeypatch.setattr(ct, "urlopen", fake_urlopen)
    body = ct.fetch_release_body("https://github.com/o/r/releases/tag/v1.0")
    assert body == "feat: real notes"
    assert captured["auth"] == "Bearer test-token-123"
