import clawbytes_threads as ct


def test_telegram_html_converts_to_slack_mrkdwn():
    html = ('⚙️ <b>Ship</b> — 2 items\n\n'
            '📦 <a href="https://github.com/o/r/releases/tag/v1">Repo v1</a> — adds &amp; fixes &quot;stuff&quot;')
    out = ct.telegram_html_to_mrkdwn(html)
    assert "*Ship*" in out
    assert "<https://github.com/o/r/releases/tag/v1|Repo v1>" in out
    assert "&amp;" in out  # Slack uses the same &/</> entity escapes — keep them
    assert '"stuff"' in out  # but &quot; is not a Slack escape — unescape it
    assert "<b>" not in out and "</a>" not in out


def test_mirror_skips_silently_without_config(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call Slack without config")

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWBYTES_SLACK_CHANNEL_ID", raising=False)
    monkeypatch.setattr(ct, "urlopen", boom)
    ct.mirror_to_slack("<b>hello</b>")  # must not raise


def test_mirror_never_raises_on_slack_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("slack is down")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("CLAWBYTES_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setattr(ct, "urlopen", boom)
    ct.mirror_to_slack("<b>hello</b>")  # must not raise
