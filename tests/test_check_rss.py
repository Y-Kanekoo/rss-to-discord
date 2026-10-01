"""外部通信と実際のWebhookなしで通知処理の既存動作を検証する。"""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_rss


def entry(index=1, **overrides):
    fields = {
        "id": f"article-{index}",
        "title": f"記事{index} | テストブログ",
        "link": f"https://example.invalid/articles/{index}",
        "description": "記事の要約",
        "published": "Thu, 01 Oct 2026 09:00:00 +0900",
        "published_parsed": (2026, 10, 1, index, 0, 0, 0, 0, 0),
    }
    fields.update(overrides)
    return feedparser.FeedParserDict(fields)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("外部通信禁止")))
        self.enterContext(patch.object(socket.socket, "connect_ex", side_effect=AssertionError("外部通信禁止")))
        self.enterContext(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS通信禁止")))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.state = Path(self.tmp) / "data" / "state.json"
        self.enterContext(patch.object(check_rss, "STATE_FILE", str(self.state)))
        self.sleep = self.enterContext(patch.object(check_rss.time, "sleep"))

    def run_main(self, entries, saved=None, error=None):
        if saved is not None:
            check_rss.save_state(str(self.state), saved)
        self.enterContext(patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.invalid/webhook"}))
        fetch = self.enterContext(patch.object(check_rss, "fetch_feed", return_value=feedparser.FeedParserDict(entries=entries)))
        send = self.enterContext(patch.object(check_rss, "send_to_discord", side_effect=error))
        check_rss.main()
        return fetch, send, check_rss.load_state(str(self.state))

    def test_missing_state_is_empty(self):
        self.assertEqual(check_rss.load_state(str(self.state)), {"sent_guids": [], "last_checked": None})

    def test_state_round_trip(self):
        expected = {"sent_guids": ["article-1"], "last_checked": "2026-10-01"}
        check_rss.save_state(str(self.state), expected)
        self.assertEqual(check_rss.load_state(str(self.state)), expected)

    def test_malformed_state_retains_existing_fallback(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{broken", encoding="utf-8")
        self.assertEqual(check_rss.load_state(str(self.state))["sent_guids"], [])

    def test_local_xml_can_be_parsed(self):
        xml = b'<rss version="2.0"><channel><title>Test</title><link>https://example.invalid</link><description>Test</description><item><title>Offline</title><link>https://example.invalid/1</link><guid>1</guid></item></channel></rss>'
        with patch.object(check_rss.feedparser, "parse", wraps=feedparser.parse) as parse:
            parsed = check_rss.fetch_feed(xml)
        parse.assert_called_once_with(xml)
        self.assertEqual(parsed.entries[0].title, "Offline")

    def test_invalid_feed_fails(self):
        invalid = feedparser.FeedParserDict(bozo=True, entries=[], bozo_exception=ValueError("invalid"))
        with patch.object(check_rss.feedparser, "parse", return_value=invalid):
            with self.assertRaises(RuntimeError):
                check_rss.fetch_feed("https://example.invalid/rss")

    def test_embed_preserves_title_source_date(self):
        embed = check_rss.build_embed(entry())
        self.assertEqual(embed["title"], "記事1")
        self.assertEqual(embed["footer"], {"text": "テストブログ"})
        self.assertEqual(embed["timestamp"], "2026-10-01T09:00:00+09:00")

    def test_embed_limits_description_and_excludes_data_image(self):
        embed = check_rss.build_embed(entry(description="a" * 500, enclosures=[{"url": "data:image/png;base64,AA=="}]))
        self.assertEqual(len(embed["description"]), 300)
        self.assertTrue(embed["description"].endswith("..."))
        self.assertNotIn("thumbnail", embed)

    def test_send_constructs_expected_request(self):
        response = Mock(status_code=204)
        with patch.object(check_rss.requests, "post", return_value=response) as post:
            check_rss.send_to_discord("https://example.invalid/webhook", {"title": "Offline"})
        post.assert_called_once_with("https://example.invalid/webhook", json={"embeds": [{"title": "Offline"}]}, headers={"Content-Type": "application/json"}, timeout=30)
        response.raise_for_status.assert_called_once()

    def test_requests_prepares_webhook_payload_without_credentials(self):
        # 実際のRequestsのシリアライズを検証する。送信や環境の認証情報参照はしない。
        with requests.Session() as session:
            session.trust_env = False
            prepared = session.prepare_request(requests.Request(
                "POST", "https://example.invalid/webhook", json={"embeds": [{"title": "検証"}]}
            ))
        self.assertEqual(prepared.method, "POST")
        self.assertEqual(prepared.headers["Content-Type"], "application/json")
        self.assertNotIn("Authorization", prepared.headers)
        self.assertEqual(json.loads(prepared.body), {"embeds": [{"title": "検証"}]})

    def test_rate_limit_retries_once(self):
        limited = Mock(status_code=429)
        limited.json.return_value = {"retry_after": 1.5}
        ok = Mock(status_code=204)
        with patch.object(check_rss.requests, "post", side_effect=[limited, ok]) as post:
            check_rss.send_to_discord("https://example.invalid/webhook", {})
        self.assertEqual(post.call_count, 2)
        self.sleep.assert_called_once_with(1.5)
        ok.raise_for_status.assert_called_once()

    def test_missing_webhook_exits_before_fetch(self):
        with patch.object(check_rss, "fetch_feed") as fetch:
            with self.assertRaises(SystemExit) as error:
                check_rss.main()
        self.assertEqual(error.exception.code, 1)
        fetch.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_first_run_only_sends_latest_five(self):
        _, send, state = self.run_main([entry(i) for i in range(1, 8)])
        self.assertEqual(send.call_count, 5)
        self.assertEqual(send.call_args_list[0].args[1]["title"], "記事3")
        self.assertEqual(set(state["sent_guids"]), {f"article-{i}" for i in range(1, 8)})

    def test_already_sent_article_is_skipped(self):
        _, send, state = self.run_main([entry()], {"sent_guids": ["article-1"], "last_checked": "2026-09-30"})
        send.assert_not_called()
        self.assertEqual(state["sent_guids"], ["article-1"])

    def test_failed_delivery_remains_retryable(self):
        _, send, state = self.run_main([entry()], {"sent_guids": [], "last_checked": "2026-09-30"}, requests.HTTPError("mock HTTP failure"))
        send.assert_called_once()
        self.assertEqual(state["sent_guids"], [])

    def test_partial_failure_records_only_success(self):
        _, send, state = self.run_main([entry(1), entry(2)], {"sent_guids": [], "last_checked": "2026-09-30"}, [requests.HTTPError("mock HTTP failure"), None])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(state["sent_guids"], ["article-2"])

    def test_fetch_failure_does_not_write_state(self):
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.invalid/webhook"}), patch.object(check_rss, "fetch_feed", side_effect=RuntimeError("mock fetch failure")), patch.object(check_rss, "send_to_discord") as send:
            with self.assertRaises(SystemExit) as error:
                check_rss.main()
        self.assertEqual(error.exception.code, 1)
        send.assert_not_called()
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
