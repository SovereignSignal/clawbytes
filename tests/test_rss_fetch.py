"""fetch_feed must read feeds whose hosts gzip the body unasked."""
import gzip
import importlib.util
from email.message import Message
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("claw_rss_monitor", SCRIPTS / "claw-rss-monitor.py")
rss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rss)

FEED = '<?xml version="1.0"?><rss><channel><title>t</title></channel></rss>'


class _Response:
    def __init__(self, body, encoding=None):
        self._body = body
        self.headers = Message()
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, response):
    monkeypatch.setattr(rss, "urlopen", lambda req, timeout=None: response)


def test_gzip_body_with_header_is_decompressed(monkeypatch):
    _serve(monkeypatch, _Response(gzip.compress(FEED.encode()), "gzip"))
    assert rss.fetch_feed("https://example.com/feed") == FEED


def test_gzip_body_without_header_is_decompressed(monkeypatch):
    _serve(monkeypatch, _Response(gzip.compress(FEED.encode())))
    assert rss.fetch_feed("https://example.com/feed") == FEED


def test_plain_body_is_unchanged(monkeypatch):
    _serve(monkeypatch, _Response(FEED.encode()))
    assert rss.fetch_feed("https://example.com/feed") == FEED
