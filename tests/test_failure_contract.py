"""Failure-contract regression tests: every network edge is blocked or mocked."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_rss
from test_check_rss import entry


class FailureContractTests(unittest.TestCase):
    def setUp(self):
        for name in ('connect', 'connect_ex'):
            self.enterContext(patch.object(socket.socket, name, side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')))
        self.enterContext(patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': 'https://example.invalid/secret-token'}, clear=True))
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / 'state.json'
        self.enterContext(patch.object(check_rss, 'STATE_FILE', str(self.path)))
        self.enterContext(patch.object(check_rss.time, 'sleep'))
        self.path.write_text(json.dumps({'sent_guids': ['old'], 'last_checked': '2026-10-01'}))

    def run_feed(self, entries, results):
        with patch.object(check_rss, 'fetch_feed', return_value=feedparser.FeedParserDict(entries=entries)), patch.object(check_rss, 'send_to_discord', side_effect=results) as send:
            code = check_rss.main()
        return code, send

    def test_partial_failure_has_nonzero_result_and_keeps_success(self):
        code, _ = self.run_feed([entry(1), entry(2)], [requests.HTTPError('secret-token'), None])
        self.assertEqual(code, 1)
        self.assertEqual(set(check_rss.load_state(str(self.path))['sent_guids']), {'old', 'article-2'})

    def test_corrupt_state_stops_without_send_or_replacement(self):
        self.path.write_text('{broken')
        with patch.object(check_rss, 'fetch_feed') as fetch, patch.object(check_rss, 'send_to_discord') as send:
            self.assertEqual(check_rss.main(), 1)
        fetch.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.path.read_text(), '{broken')

    def test_invalid_state_types_fail_closed(self):
        for value in ([], None, {}, {'sent_guids': 'abc', 'last_checked': None}, {'sent_guids': [None], 'last_checked': None}, {'sent_guids': [[]], 'last_checked': None}, {'sent_guids': [], 'last_checked': 5}):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    check_rss.load_state(str(self.path))

    def test_atomic_save_failure_preserves_existing_bytes(self):
        original = self.path.read_bytes()
        with patch.object(check_rss.json, 'dump', side_effect=OSError('secret-token')):
            with self.assertRaises(OSError):
                check_rss.save_state(str(self.path), {'sent_guids': ['new'], 'last_checked': None})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_duplicate_guids_attempted_only_once_per_run(self):
        _, send = self.run_feed([entry(1), entry(1)], [requests.HTTPError('secret-token'), None])
        self.assertEqual(send.call_count, 1)

    def test_delivery_failure_does_not_print_exception_secret(self):
        self.run_feed([entry(1)], [requests.ConnectionError('https://example.invalid/secret-token')])
        self.assertNotIn('secret-token', self.out.getvalue() + self.err.getvalue())

    def test_unknown_delivery_returns_nonzero_without_immediate_retry(self):
        code, send = self.run_feed([entry(1)], [requests.Timeout('secret-token')])
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn('article-1', check_rss.load_state(str(self.path))['sent_guids'])

    def test_rerun_sends_only_previous_failure(self):
        self.run_feed([entry(1), entry(2)], [None, requests.HTTPError('failed')])
        code, send = self.run_feed([entry(1), entry(2)], [None])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1]['title'], '記事2')

    def test_failure_emits_machine_readable_summary(self):
        self.run_feed([entry(1)], [requests.HTTPError('failed')])
        result = json.loads(self.out.getvalue().splitlines()[-1])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['sent'], 0)
        self.assertEqual(result['failed'], 1)

    def test_invalid_utf8_and_duplicate_json_keys_fail_closed(self):
        for content in (b'\xff', b'{"sent_guids":[],"sent_guids":["old"],"last_checked":null}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(check_rss.StateError):
                    check_rss.load_state(str(self.path))

    def test_state_read_permission_failure_is_safe(self):
        with patch('builtins.open', side_effect=PermissionError('secret-token')):
            with self.assertRaises(check_rss.StateError):
                check_rss.load_state(str(self.path))

    def test_atomic_replace_failure_preserves_old_and_cleans_temporary(self):
        original = self.path.read_bytes()
        with patch.object(check_rss.os, 'replace', side_effect=OSError('secret-token')):
            with self.assertRaises(OSError):
                check_rss.save_state(str(self.path), {'sent_guids': [], 'last_checked': None})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_unwritable_state_blocks_all_sends(self):
        original = self.path.read_bytes()
        with patch.object(check_rss, 'save_state', side_effect=OSError('secret-token')):
            code, send = self.run_feed([entry(1)], [None])
        self.assertEqual(code, 1)
        send.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn('secret-token', self.out.getvalue() + self.err.getvalue())

    def test_checkpoint_failure_stops_next_send_and_reports_acknowledged_gap(self):
        original_save = check_rss.save_state
        saves = 0
        def save(path, state):
            nonlocal saves
            saves += 1
            if saves == 3:  # preflight, success 1, then success 2 cannot be saved
                raise OSError('secret-token')
            return original_save(path, state)
        with patch.object(check_rss, 'save_state', side_effect=save):
            code, send = self.run_feed([entry(1), entry(2), entry(3)], [None, None, None])
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(set(check_rss.load_state(str(self.path))['sent_guids']), {'old', 'article-1'})
        summary = json.loads(self.out.getvalue().splitlines()[-1])
        self.assertEqual(summary['unpersisted_sent'], 1)
        self.assertEqual(summary['sent'], 2)

    def test_first_run_failed_article_is_not_skipped_by_later_first_run_cap(self):
        self.path.unlink()
        code, send = self.run_feed([entry(i) for i in range(1, 8)], [requests.HTTPError('failed')] * 5)
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 5)
        code, send = self.run_feed([entry(i) for i in range(1, 11)], [None] * 8)
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 8)
        self.assertEqual(send.call_args_list[0].args[1]['title'], '記事3')

    def test_explicit_dry_run_needs_no_webhook_and_never_writes_or_sends(self):
        original = self.path.read_bytes()
        with patch.dict(os.environ, {}, clear=True), patch.object(check_rss, 'fetch_feed', return_value=feedparser.FeedParserDict(entries=[entry(1)])), patch.object(check_rss, 'send_to_discord') as send, patch.object(check_rss, 'save_state') as save:
            self.assertEqual(check_rss.main(dry_run=True), 0)
        send.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(json.loads(self.out.getvalue())['status'], 'dry_run')

    def test_blank_webhook_is_failure(self):
        with patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': '  '}), patch.object(check_rss, 'fetch_feed') as fetch:
            self.assertEqual(check_rss.main(), 1)
        fetch.assert_not_called()
        self.assertEqual(json.loads(self.out.getvalue())['errors'], ['missing_webhook'])

    def test_feed_exception_is_redacted_and_does_not_write(self):
        original = self.path.read_bytes()
        with patch.object(check_rss, 'fetch_feed', side_effect=ValueError('secret-token')):
            self.assertEqual(check_rss.main(), 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn('secret-token', self.out.getvalue() + self.err.getvalue())

    def test_http_feed_error_is_not_empty_success(self):
        feed = feedparser.FeedParserDict(status=503, bozo=False, entries=[])
        with patch.object(check_rss.feedparser, 'parse', return_value=feed):
            self.assertEqual(check_rss.main(), 1)

    def test_two_rate_limits_fail_after_one_retry(self):
        limited = Mock(status_code=429)
        limited.json.return_value = {'retry_after': 0.1}
        limited.raise_for_status.side_effect = requests.HTTPError('secret-token')
        with patch.object(check_rss.requests, 'post', side_effect=[limited, limited]) as post:
            with self.assertRaises(requests.HTTPError):
                check_rss.send_to_discord('https://example.invalid/secret-token', {})
        self.assertEqual(post.call_count, 2)

    def test_invalid_retry_after_is_not_slept_or_retried(self):
        for value in (-1, 61, float('nan'), float('inf'), 'secret-token', True, {}):
            with self.subTest(value=value):
                response = Mock(status_code=429)
                response.json.return_value = {'retry_after': value}
                with patch.object(check_rss.requests, 'post', return_value=response) as post, patch.object(check_rss.time, 'sleep') as sleep:
                    with self.assertRaises(ValueError):
                        check_rss.send_to_discord('https://example.invalid/secret-token', {})
                    post.assert_called_once()
                    sleep.assert_not_called()

    def test_redirect_is_not_accepted_as_delivery(self):
        with patch.object(check_rss.requests, 'post', return_value=Mock(status_code=302)) as post:
            with self.assertRaises(requests.HTTPError):
                check_rss.send_to_discord('https://example.invalid/secret-token', {})
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_invalid_entry_does_not_prevent_other_success(self):
        code, send = self.run_feed([entry(1, id=None, link=None), entry(2, title=None), entry(3)], [None])
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(set(check_rss.load_state(str(self.path))['sent_guids']), {'old', 'article-3'})

    def test_non_feed_document_is_not_no_news_success(self):
        with patch.object(check_rss.feedparser, 'parse', return_value=feedparser.parse(b'<html><body>Unavailable</body></html>')):
            self.assertEqual(check_rss.main(), 1)
        self.assertEqual(json.loads(self.out.getvalue())['errors'], ['feed_unavailable_or_invalid'])

    def test_valid_empty_feed_is_success(self):
        xml = b'<rss version="2.0"><channel><title>Empty</title><link>https://example.invalid</link><description>Empty</description></channel></rss>'
        with patch.object(check_rss.feedparser, 'parse', return_value=feedparser.parse(xml)), patch.object(check_rss, 'send_to_discord') as send:
            self.assertEqual(check_rss.main(), 0)
        send.assert_not_called()

    def test_invalid_timestamp_stops_before_fetch_or_send(self):
        self.path.write_text(json.dumps({'sent_guids': ['old'], 'last_checked': 'not-a-timestamp'}))
        original = self.path.read_bytes()
        with patch.object(check_rss, 'fetch_feed') as fetch, patch.object(check_rss, 'send_to_discord') as send:
            self.assertEqual(check_rss.main(), 1)
        fetch.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
