"""Exercise the real __main__ exit code with mocked HTTP in a child process."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = r'''
import json, os, runpy, socket, sys
from unittest.mock import patch
import feedparser, requests

def blocked(*args, **kwargs):
    raise AssertionError('Network forbidden')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
responses = json.loads(os.environ['TEST_RESPONSES'])
posts = []
entries = [feedparser.FeedParserDict(id=str(i), title='Article', link='https://example.invalid/' + str(i)) for i in [1, 2, 2]]
def post(url, **kwargs):
    posts.append(kwargs['json']['embeds'][0]['url'])
    value = responses.pop(0)
    if value == 'timeout':
        raise requests.Timeout('https://example.invalid/secret-token')
    response = requests.Response()
    response.status_code = value
    response.url = 'https://example.invalid/secret-token'
    response._content = b'{"retry_after": 0}'
    return response
sys.argv = [os.environ['TEST_SCRIPT']] + json.loads(os.environ.get('TEST_ARGS', '[]'))
try:
    with patch.object(feedparser, 'parse', return_value=feedparser.FeedParserDict(bozo=False, version="rss20", entries=entries)), patch.object(requests, 'post', side_effect=post), patch('time.sleep'):
        runpy.run_path(sys.argv[0], run_name='__main__')
finally:
    print(json.dumps({'posts': posts}), file=sys.stderr)
'''


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'data').mkdir()
        shutil.copy(ROOT / 'scripts/check_rss.py', self.root / 'scripts/check_rss.py')
        self.state = self.root / 'data/sent_articles.json'
        self.state.write_text(json.dumps({'sent_guids': [], 'last_checked': '2026-10-01'}))

    def run_cli(self, responses, args=(), webhook=True):
        env = {'PATH': os.environ.get('PATH', ''), 'TEST_SCRIPT': str(self.root / 'scripts/check_rss.py'),
               'TEST_RESPONSES': json.dumps(responses), 'TEST_ARGS': json.dumps(args)}
        if webhook:
            env['DISCORD_WEBHOOK_URL'] = 'https://example.invalid/secret-token'
        result = subprocess.run([sys.executable, '-c', WRAPPER], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotIn('secret-token', result.stdout + result.stderr)
        return result, json.loads(result.stdout), json.loads(result.stderr.splitlines()[-1])['posts']

    def test_partial_failure_real_exit_and_rerun_idempotency(self):
        result, summary, posts = self.run_cli([204, 500])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(summary['sent'], 1)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(len(posts), 2)
        self.assertEqual(json.loads(self.state.read_text())['sent_guids'], ['1'])
        result, summary, posts = self.run_cli([204])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(posts, ['https://example.invalid/2'])
        self.assertEqual(json.loads(self.state.read_text())['sent_guids'], ['1', '2'])

    def test_429_success_then_unknown_response(self):
        result, summary, posts = self.run_cli([429, 204, 'timeout'])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(summary['delivery_unknown'], 1)
        self.assertEqual(len(posts), 3)
        self.assertEqual(json.loads(self.state.read_text())['sent_guids'], ['1'])

    def test_429_retry_failure_is_nonzero(self):
        result, summary, posts = self.run_cli([429, 429, 204])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(len(posts), 3)
        self.assertEqual(json.loads(self.state.read_text())['sent_guids'], ['2'])

    def test_corruption_preserves_original_bytes(self):
        self.state.write_text('{broken')
        result, summary, posts = self.run_cli([])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(summary['errors'], ['state_unreadable_or_invalid'])
        self.assertEqual(posts, [])
        self.assertEqual(self.state.read_text(), '{broken')

    def test_missing_webhook_and_explicit_dry_run_are_distinct(self):
        original = self.state.read_bytes()
        result, summary, posts = self.run_cli([], webhook=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(summary['errors'], ['missing_webhook'])
        result, summary, posts = self.run_cli([], ['--dry-run'], webhook=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(summary['status'], 'dry_run')
        self.assertEqual(summary['selected'], 2)
        self.assertEqual(posts, [])
        self.assertEqual(self.state.read_bytes(), original)
