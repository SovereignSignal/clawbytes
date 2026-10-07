"""Send-path sanitizer: bare domains, @handles, and raw markdown.

Posts #963, #970, #920, and #968 are the fixtures. Intended <a href> titles
stay links. A second pass does not wrap twice.
"""
import ss_publish
from ss_publish import Publisher, sanitize_telegram_html


def test_real_posts_lose_autolinks_and_raw_markdown():
    samples = {
        # #963
        "listens on 127.0.0.1 only": "<code>127.0.0.1</code>",
        "the Z.ai Coding Plan": "<code>Z.ai</code>",
        # #965 #967 #971 #974
        "see claude.dev today": "<code>claude.dev</code>",
        "wikimedia.org and vals.ai": "<code>wikimedia.org</code>",
        "docs.mistral.ai/models/mistral-large-4-0": "<code>docs.mistral.ai/models/mistral-large-4-0</code>",
        "cited science.org.": "<code>science.org</code>.",
        "github.com/fesens": "<code>github.com/fesens</code>",
        # #970
        "rendering as named @group mentions": "<code>@group</code>",
        # #920 #924 #964 #968
        "run `codex mcp add --oauth-client-secret` now": "<code>codex mcp add --oauth-client-secret</code>",
        "set `thinking: {type: between_tools}`": "<code>thinking: {type: between_tools}</code>",
        "call `GET /v1/models`": "<code>GET /v1/models</code>",
        "use `devin --cloud` / `/cloud`": "<code>devin --cloud</code>",
        "this is **bold**": "<b>bold</b>",
    }
    for raw, needle in samples.items():
        cleaned = sanitize_telegram_html(raw)
        assert needle in cleaned, raw
        assert sanitize_telegram_html(cleaned) == cleaned


def test_intended_anchor_survives_and_versions_are_not_code():
    raw = (
        '📦 <a href="https://docs.devin.ai/cli/changelog/stable#updated-db37b8f3">'
        "Devin CLI v3000.11.1</a> — listens on 127.0.0.1 only, version 4.5"
    )
    cleaned = sanitize_telegram_html(raw)
    assert '<a href="https://docs.devin.ai/cli/changelog/stable#updated-db37b8f3">Devin CLI v3000.11.1</a>' in cleaned
    assert "<code>127.0.0.1</code>" in cleaned
    assert "v3000.11.1</a>" in cleaned
    assert "<code>4.5</code>" not in cleaned
    assert "<code>v3000" not in cleaned


def test_send_telegram_sanitizes_before_post():
    sent = {}

    def _post(url, **kwargs):
        sent["text"] = kwargs["json"]["text"]

        class _Resp:
            status_code = 200
            headers = {}
            text = ""

            def json(self):
                return {"ok": True, "result": {"message_id": 7}}

        return _Resp()

    pub = Publisher(
        telegram_token="fake",
        telegram_channel_id="-100",
        _post=_post,
        _sleep=lambda *a, **k: None,
    )
    result = pub.send_telegram("listens on 127.0.0.1 only and `GET /v1/models`")
    assert result.ok
    assert "<code>127.0.0.1</code>" in sent["text"]
    assert "<code>GET /v1/models</code>" in sent["text"]
    assert "`GET" not in sent["text"]
    mrkdwn = ss_publish.telegram_html_to_mrkdwn(sent["text"])
    assert "`127.0.0.1`" in mrkdwn
    assert "`GET /v1/models`" in mrkdwn
