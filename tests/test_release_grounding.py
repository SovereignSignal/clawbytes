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


def test_changelog_md_strips_preamble_and_rejects_html(monkeypatch):
    import io

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # Markdown with a Mintlify "Documentation Index" blockquote preamble
    md = ("> ## Documentation Index\n> Fetch the full index at https://x/llms.txt\n> Use this file.\n\n"
          "# Recent Updates\n<Update label=\"June 12\">\n**Session Folders** — group sessions.\n</Update>")
    monkeypatch.setattr(ct, "urlopen", lambda req, timeout=0: FakeResp(md.encode()))
    out = ct.fetch_changelog_markdown("https://docs.devin.ai/release-notes/overview#updated-abc")
    assert out.startswith("# Recent Updates")          # preamble stripped
    assert "Documentation Index" not in out
    assert "Session Folders" in out

    # HTML shell must be rejected (returns '')
    monkeypatch.setattr(ct, "urlopen", lambda req, timeout=0: FakeResp(b"<!DOCTYPE html><html><head></head></html>"))
    assert ct.fetch_changelog_markdown("https://docs.factory.ai/changelog") == ""


def test_looks_like_changelog():
    assert ct._looks_like_changelog("https://docs.devin.ai/release-notes/overview")
    assert ct._looks_like_changelog("https://docs.factory.ai/changelog")
    assert not ct._looks_like_changelog("https://huggingface.co/papers/2606.1")
    assert not ct._looks_like_changelog("https://news.ycombinator.com/item?id=1")
