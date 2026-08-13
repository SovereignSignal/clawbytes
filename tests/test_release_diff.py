"""Tests for opt-in release changelog diffing (CLAWBYTES_RELEASE_DIFF).

Turns a thin "vX released" release body into "changes since vX-1" grounding,
falling back to commit subjects from the compare endpoint when the maintainer
wrote no notes. Off by default: fetch_release_body is unchanged unless the flag
is set.
"""
import io
import json

import clawbytes_threads as ct


RELEASES_NEWEST_FIRST = [
    {"tag_name": "v1.13.0"},
    {"tag_name": "v1.12.1", "prerelease": True},
    {"tag_name": "v1.12.0"},
    {"tag_name": "v1.11.0", "draft": True},
    {"tag_name": "v1.10.0"},
]


def test_previous_release_tag_skips_prereleases_and_drafts():
    assert ct._previous_release_tag(RELEASES_NEWEST_FIRST, "v1.13.0") == "v1.12.0"
    # v1.11.0 is a draft so it is not even a candidate; prev of v1.12.0 = v1.10.0
    assert ct._previous_release_tag(RELEASES_NEWEST_FIRST, "v1.12.0") == "v1.10.0"


def test_previous_release_tag_edges():
    assert ct._previous_release_tag(RELEASES_NEWEST_FIRST, "v9.9.9") is None  # unknown
    assert ct._previous_release_tag(RELEASES_NEWEST_FIRST, "v1.10.0") is None  # oldest
    assert ct._previous_release_tag([], "v1.0.0") is None


def test_compare_commit_lines_filters_and_dedupes():
    comp = {"commits": [
        {"commit": {"message": "feat: streaming tool calls (#12)\n\nlong body"}},
        {"commit": {"message": "Merge pull request #13 from x/y"}},
        {"commit": {"message": "chore(deps): bump dependabot thing"}},
        {"commit": {"message": "fix: PTY resize crash"}},
        {"commit": {"message": "feat: streaming tool calls (#99)"}},  # dup subject after (#) strip
    ]}
    out = ct._compare_commit_lines(comp)
    assert out == "- feat: streaming tool calls\n- fix: PTY resize crash"


def test_compare_commit_lines_drops_release_bot_churn():
    comp = {"commits": [
        {"commit": {"message": "chore(release): release version 1.34.1 (patch)"}},
        {"commit": {"message": 'Revert "chore(release): release version 1.34.1 (patch)"'}},
        {"commit": {"message": "Build non-vulkan linux variants using ubuntu 22.04 (#9211)"}},
    ]}
    out = ct._compare_commit_lines(comp)
    assert out == "- Build non-vulkan linux variants using ubuntu 22.04"


def test_compare_commit_lines_respects_limit():
    comp = {"commits": [{"commit": {"message": f"feat: thing {i}"}} for i in range(20)]}
    out = ct._compare_commit_lines(comp, limit=3)
    assert out.count("\n") == 2  # 3 bullets


def test_compose_release_diff_shapes():
    # substantial body -> header + body, no commits
    assert ct._compose_release_diff("real notes", "v1.2.0", "") == "(changes since v1.2.0)\nreal notes"
    # thin body -> header + commits
    out = ct._compose_release_diff("", "v1.2.0", "- feat: x")
    assert out == "(changes since v1.2.0)\nCommits since v1.2.0:\n- feat: x"


def test_compose_release_diff_truncates():
    assert len(ct._compose_release_diff("x" * 2000, "v1", "")) == 900


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_release_body_unchanged_when_flag_off(monkeypatch):
    monkeypatch.delenv("CLAWBYTES_RELEASE_DIFF", raising=False)

    def only_tag(req, timeout=0):
        assert "/releases/tags/" in req.full_url  # must NOT hit releases list/compare
        return _FakeResp(json.dumps({"body": "feat: real notes"}).encode())

    monkeypatch.setattr(ct, "urlopen", only_tag)
    assert ct.fetch_release_body("https://github.com/o/r/releases/tag/v1.13.0") == "feat: real notes"


def test_fetch_release_body_augments_thin_notes_with_commits(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_RELEASE_DIFF", "1")

    def route(req, timeout=0):
        u = req.full_url
        if "/releases/tags/" in u:
            return _FakeResp(json.dumps({"body": ""}).encode())  # thin/empty notes
        if "/releases?per_page=" in u:
            return _FakeResp(json.dumps(RELEASES_NEWEST_FIRST).encode())
        if "/compare/v1.12.0...v1.13.0" in u:
            return _FakeResp(json.dumps({"commits": [
                {"commit": {"message": "feat: streaming tool calls (#12)"}},
                {"commit": {"message": "Merge pull request #13"}},
            ]}).encode())
        raise AssertionError(f"unexpected url {u}")

    monkeypatch.setattr(ct, "urlopen", route)
    out = ct.fetch_release_body("https://github.com/o/r/releases/tag/v1.13.0")
    assert "changes since v1.12.0" in out
    assert "streaming tool calls" in out
    assert "Merge pull request" not in out


def test_fetch_release_body_substantial_notes_get_prev_header_only(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_RELEASE_DIFF", "1")
    big = "feat: " + "a real substantial changelog line " * 5  # >= 80 chars

    def route(req, timeout=0):
        u = req.full_url
        if "/releases/tags/" in u:
            return _FakeResp(json.dumps({"body": big}).encode())
        if "/releases?per_page=" in u:
            return _FakeResp(json.dumps(RELEASES_NEWEST_FIRST).encode())
        raise AssertionError(f"compare should not be fetched for substantial notes: {u}")

    monkeypatch.setattr(ct, "urlopen", route)
    out = ct.fetch_release_body("https://github.com/o/r/releases/tag/v1.13.0")
    assert out.startswith("(changes since v1.12.0)")
    assert "substantial changelog line" in out


def test_fetch_release_body_degrades_when_releases_list_fails(monkeypatch):
    monkeypatch.setenv("CLAWBYTES_RELEASE_DIFF", "1")

    def route(req, timeout=0):
        u = req.full_url
        if "/releases/tags/" in u:
            return _FakeResp(json.dumps({"body": "feat: real notes"}).encode())
        raise OSError("network down for releases list")

    monkeypatch.setattr(ct, "urlopen", route)
    # augmentation fails -> falls back to the plain cleaned body, never raises
    assert ct.fetch_release_body("https://github.com/o/r/releases/tag/v1.13.0") == "feat: real notes"
