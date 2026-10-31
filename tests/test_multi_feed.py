"""複数フィードの状態移行、独立処理、機密性をオフラインで検証する。"""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import call, patch

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_rss
import persist_state


class MultiFeedTests(unittest.TestCase):
    def setUp(self):
        for method in ("connect", "connect_ex"):
            self.enterContext(patch.object(socket.socket, method, side_effect=AssertionError("外部通信禁止")))
        self.enterContext(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS通信禁止")))
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / "state.json"
        self.enterContext(patch.object(check_rss, "STATE_FILE", str(self.path)))
        self.enterContext(patch.object(check_rss.time, "sleep"))
        self.feed_configs = [
            {"id": "ja-blog", "name": "ブログ", "url": "https://example.invalid/blog",
             "category": "ja_blog", "webhook_env": "DISCORD_WEBHOOK_BLOG",
             "max_articles_first_run": 5, "rate_limit_interval": 0},
            {"id": "release-tool", "name": "Tool", "url": "https://example.invalid/release",
             "category": "release", "webhook_env": "DISCORD_WEBHOOK_RELEASE",
             "max_articles_first_run": 2, "rate_limit_interval": 0},
        ]
        self.real_load_config = check_rss.load_feeds_config
        self.enterContext(patch.object(check_rss, "load_feeds_config", return_value=self.feed_configs))
        self.enterContext(patch.dict(os.environ, {
            "DISCORD_WEBHOOK_URL": "https://example.invalid/legacy-secret",
            "DISCORD_WEBHOOK_RELEASE": "https://example.invalid/release-secret",
        }, clear=True))
        self.sample = feedparser.parse(
            (Path(__file__).parent / "fixtures/sample_feed.xml").read_bytes()
        )

    def test_legacy_state_migrates_without_losing_blog_guids(self):
        self.path.write_text(json.dumps({"sent_guids": ["sample-1"], "last_checked": "2026-10-01"}))
        with patch.object(check_rss, "fetch_feed", return_value=self.sample), patch.object(check_rss, "send_to_discord") as send:
            self.assertEqual(check_rss.main(), 0)
        state = check_rss.load_state(str(self.path))
        self.assertEqual(state["version"], 2)
        self.assertEqual(set(state["sent_guids"]), {"sample-1", "sample-2", "sample-3"})
        self.assertEqual(set(state["feeds"]["release-tool"]["sent_guids"]), {"sample-1", "sample-2", "sample-3"})
        self.assertEqual(send.call_count, 4)

    def test_feed_failure_does_not_block_other_feed_but_exits_nonzero(self):
        def fetch(url):
            if url.endswith("blog"):
                raise requests.Timeout("https://example.invalid/private-feed")
            return self.sample
        with patch.object(check_rss, "fetch_feed", side_effect=fetch), patch.object(check_rss, "send_to_discord") as send:
            self.assertEqual(check_rss.main(), 1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(check_rss.load_state(str(self.path))["feeds"]["release-tool"]["sent_guids"], ["sample-1", "sample-2", "sample-3"])
        self.assertIn("feed_unavailable_or_invalid", json.loads(self.out.getvalue())["errors"])
        self.assertNotIn("private-feed", self.out.getvalue() + self.err.getvalue())

    def test_http_error_never_logs_webhook_url_or_entry_content(self):
        secret = "https://example.invalid/legacy-secret"
        self.path.write_text(json.dumps({"sent_guids": [], "last_checked": "2026-10-01"}))
        response = requests.Response()
        response.status_code = 500
        response.url = secret
        response._content = b"private-response-body"
        success = requests.Response()
        success.status_code = 204
        def post(url, **kwargs):
            return response if url == secret else success
        with patch.object(check_rss, "fetch_feed", return_value=self.sample), patch.object(check_rss.requests, "post", side_effect=post):
            self.assertEqual(check_rss.main(), 1)
        logs = self.out.getvalue() + self.err.getvalue()
        for forbidden in (secret, "legacy-secret", "release-secret", "private-response-body", "最新記事", "sample-1"):
            self.assertNotIn(forbidden, logs)
        self.assertEqual(json.loads(self.out.getvalue())["failed"], 3)
        self.assertEqual(json.loads(self.out.getvalue())["sent"], 2)

    def test_persistence_merges_each_feed_without_losing_legacy_history(self):
        remote = {"sent_guids": ["legacy"], "last_checked": "2026-10-01"}
        local = {"version": 2, "sent_guids": ["new"], "last_checked": "2026-10-02",
                 "feeds": {"release-tool": {"sent_guids": ["r1"], "last_checked": "2026-10-02"}}}
        merged = persist_state.merge_states(remote, local)
        self.assertEqual(merged["sent_guids"], ["legacy", "new"])
        self.assertEqual(merged["feeds"]["release-tool"]["sent_guids"], ["r1"])
        remote2 = {"version": 2, "sent_guids": ["legacy"], "last_checked": "2026-10-01",
                   "feeds": {"release-tool": {"sent_guids": ["r2"], "last_checked": "2026-10-03"}}}
        merged = persist_state.merge_states(remote2, local)
        self.assertEqual(merged["feeds"]["release-tool"]["sent_guids"], ["r1", "r2"])
        self.assertEqual(merged["feeds"]["release-tool"]["last_checked"], "2026-10-03")

    def test_arxiv_filter_and_category_embed(self):
        self.assertTrue(check_rss.is_arxiv_match(feedparser.FeedParserDict(title="Automated Testing for LLMs")))
        self.assertFalse(check_rss.is_arxiv_match(self.sample.entries[2]))
        self.assertFalse(check_rss.is_arxiv_match(feedparser.FeedParserDict(
            title="Image Classification", summary="coverage was measured")))
        self.assertTrue(check_rss.is_arxiv_match(feedparser.FeedParserDict(
            title="Image Classification", summary="fuzzing and mutation were measured")))
        release = check_rss.build_embed(self.sample.entries[0], self.feed_configs[1])
        self.assertTrue(release["title"].startswith("Tool: "))
        self.assertEqual(release["color"], check_rss.CATEGORY_COLORS["release"])

    def test_atom_date_and_youtube_thumbnail(self):
        atom = feedparser.FeedParserDict(title="動画", link="https://example.invalid/watch",
            updated="2026-10-31T09:00:00Z",
            media_thumbnail=[{"url": "https://example.invalid/thumbnail.png"}])
        video = {**self.feed_configs[1], "category": "youtube", "name": "JaSST"}
        embed = check_rss.build_embed(atom, video)
        self.assertEqual(embed["timestamp"], "2026-10-31T09:00:00+00:00")
        self.assertEqual(embed["thumbnail"], {"url": "https://example.invalid/thumbnail.png"})

    def test_missing_optional_webhook_is_warning_and_keeps_legacy_operation(self):
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_RELEASE": ""}), patch.object(
            check_rss, "fetch_feed", return_value=self.sample), patch.object(
            check_rss, "send_to_discord") as send:
            self.assertEqual(check_rss.main(), 0)
        self.assertEqual(send.call_count, 3)
        self.assertIn("WARNING: missing_webhook feed=release-tool", self.err.getvalue())
        self.assertNotIn("release-tool", check_rss.load_state(str(self.path))["feeds"])

    def test_invalid_multifeed_state_fails_closed(self):
        original = b'{"version":2,"sent_guids":[],"last_checked":null,"feeds":{"release-tool":{"sent_guids":"bad","last_checked":null}}}'
        self.path.write_bytes(original)
        with patch.object(check_rss, "fetch_feed") as fetch, patch.object(check_rss, "send_to_discord") as send:
            self.assertEqual(check_rss.main(), 1)
        fetch.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_repository_config_contains_distinct_enabled_feeds(self):
        config = self.real_load_config(str(Path(__file__).resolve().parents[1] / "config/feeds.yml"))
        self.assertEqual(len(config), 8)
        self.assertEqual(len({feed["id"] for feed in config}), 8)
        youtube = next(feed for feed in config if feed["id"] == "youtube-jasst")
        self.assertIn("channel_id=", youtube["url"])

    def test_fetch_timeout_is_restored_after_feed_error(self):
        previous = socket.getdefaulttimeout()
        with patch.object(check_rss.feedparser, "parse", side_effect=ValueError("private-token")), patch.object(
            check_rss.socket, "setdefaulttimeout", wraps=socket.setdefaulttimeout) as set_timeout:
            with self.assertRaises(ValueError):
                check_rss.fetch_feed("https://example.invalid/feed")
        self.assertEqual(set_timeout.call_args_list, [call(check_rss.FETCH_TIMEOUT), call(previous)])
        self.assertEqual(socket.getdefaulttimeout(), previous)


if __name__ == "__main__":
    unittest.main()
