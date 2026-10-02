"""State publication uses only local bare Git remotes; no service or token."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import persist_state


class PersistStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(self.temp)
        self.enterContext(patch.dict(os.environ, {
            'PATH': os.environ.get('PATH', ''), 'HOME': self.temp,
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0',
        }, clear=True))
        for name in ('connect', 'connect_ex'):
            self.enterContext(patch.object(socket.socket, name, side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')))
        self.remote = root / 'remote.git'
        self.seed = root / 'seed'
        self.repo = root / 'checkout'
        self.git(root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        self.git(root, 'clone', str(self.remote), str(self.seed))
        self.configure(self.seed)
        self.write_state(self.seed, ['old'])
        (self.seed / 'code.txt').write_text('original code')
        self.git(self.seed, 'add', '.')
        self.git(self.seed, 'commit', '-m', 'initial')
        self.git(self.seed, 'push', 'origin', 'main')
        self.git(root, 'clone', str(self.remote), str(self.repo))
        self.configure(self.repo)
        self.write_state(self.repo, ['old', 'local-success'])

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def configure(self, root):
        self.git(root, 'config', 'user.name', 'Offline Test')
        self.git(root, 'config', 'user.email', 'test@example.invalid')

    def write_state(self, root, guids, **extra):
        path = root / persist_state.STATE_PATH
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'sent_guids': guids, 'last_checked': '2026-10-02T04:00:00+00:00', **extra}))

    def remote_state(self):
        return json.loads(self.git(self.remote, 'show', 'main:' + persist_state.STATE_PATH))

    def concurrent_commit(self, guid, code):
        state = json.loads((self.seed / persist_state.STATE_PATH).read_text())
        self.write_state(self.seed, state['sent_guids'] + [guid])
        (self.seed / 'code.txt').write_text(code)
        self.git(self.seed, 'add', '.')
        self.git(self.seed, 'commit', '-m', 'concurrent code and state')
        self.git(self.seed, 'push', 'origin', 'main')

    def test_stale_checkout_preserves_concurrent_code_and_sent_history(self):
        self.concurrent_commit('remote-success', 'new code')
        original = (self.repo / persist_state.STATE_PATH).read_bytes()
        self.assertEqual(persist_state.persist(self.repo), 'persisted')
        self.assertEqual(set(self.remote_state()['sent_guids']), {'old', 'local-success', 'remote-success'})
        self.assertEqual(self.git(self.remote, 'show', 'main:code.txt'), 'new code')
        self.assertEqual(self.git(self.remote, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'main'), persist_state.STATE_PATH)
        self.assertEqual((self.repo / persist_state.STATE_PATH).read_bytes(), original)
        self.assertEqual((self.repo / 'code.txt').read_text(), 'original code')

    def test_race_after_fetch_retries_with_union_and_keeps_latest_code(self):
        real_git = persist_state.git
        pushes = 0
        def racing_git(root, *args):
            nonlocal pushes
            if args[0] == 'push':
                pushes += 1
                if pushes == 1:
                    self.concurrent_commit('raced-success', 'raced code')
            return real_git(root, *args)
        with patch.object(persist_state, 'git', side_effect=racing_git):
            self.assertEqual(persist_state.persist(self.repo), 'persisted')
        self.assertEqual(pushes, 2)
        self.assertEqual(set(self.remote_state()['sent_guids']), {'old', 'local-success', 'raced-success'})
        self.assertEqual(self.git(self.remote, 'show', 'main:code.txt'), 'raced code')

    def test_ambiguous_push_success_does_not_create_duplicate_commit(self):
        real_git = persist_state.git
        pushed = False
        def ambiguous_git(root, *args):
            nonlocal pushed
            value = real_git(root, *args)
            if args[0] == 'push' and not pushed:
                pushed = True
                raise persist_state.PersistenceError('git_operation_failed')
            return value
        with patch.object(persist_state, 'git', side_effect=ambiguous_git):
            self.assertEqual(persist_state.persist(self.repo), 'already_persisted')
        self.assertEqual(self.git(self.remote, 'rev-list', '--count', 'main'), '2')
        self.assertEqual(set(self.remote_state()['sent_guids']), {'old', 'local-success'})

    def test_failed_push_keeps_local_history_and_validated_recovery(self):
        before = self.git(self.remote, 'rev-parse', 'main')
        original = (self.repo / persist_state.STATE_PATH).read_bytes()
        real_git = persist_state.git
        pushes = 0
        def failing_git(root, *args):
            nonlocal pushes
            if args[0] == 'push':
                pushes += 1
                raise persist_state.PersistenceError('git_operation_failed')
            return real_git(root, *args)
        with patch.object(persist_state, 'git', side_effect=failing_git):
            with self.assertRaises(persist_state.PersistenceError):
                persist_state.persist(self.repo)
        self.assertEqual(pushes, 3)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), before)
        self.assertEqual((self.repo / persist_state.STATE_PATH).read_bytes(), original)
        recovery = json.loads((self.repo / persist_state.RECOVERY_PATH).read_text())
        self.assertEqual(set(recovery['sent_guids']), {'old', 'local-success'})
        self.assertEqual(self.git(self.repo, 'worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_corrupt_remote_is_not_overwritten(self):
        (self.seed / persist_state.STATE_PATH).write_text('{broken')
        self.git(self.seed, 'commit', '-am', 'corrupt state')
        self.git(self.seed, 'push', 'origin', 'main')
        before = self.git(self.remote, 'rev-parse', 'main')
        with self.assertRaises(persist_state.PersistenceError):
            persist_state.persist(self.repo)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), before)
        self.assertEqual(self.git(self.remote, 'show', 'main:' + persist_state.STATE_PATH), '{broken')
        self.assertTrue((self.repo / persist_state.RECOVERY_PATH).exists())

    def test_invalid_local_state_is_not_published_or_archived(self):
        (self.repo / persist_state.STATE_PATH).write_text('{broken')
        before = self.git(self.remote, 'rev-parse', 'main')
        with self.assertRaises(ValueError):
            persist_state.persist(self.repo)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), before)
        self.assertFalse((self.repo / persist_state.RECOVERY_PATH).exists())

    def test_missing_local_state_does_not_reset_remote(self):
        (self.repo / persist_state.STATE_PATH).unlink()
        before = self.git(self.remote, 'rev-parse', 'main')
        self.assertEqual(persist_state.persist(self.repo), 'no_local_state')
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), before)

    def test_concurrent_remote_deletion_does_not_reset_history(self):
        self.git(self.seed, 'rm', persist_state.STATE_PATH)
        self.git(self.seed, 'commit', '-m', 'concurrent removal')
        self.git(self.seed, 'push', 'origin', 'main')
        before = self.git(self.remote, 'rev-parse', 'main')
        with self.assertRaises(persist_state.PersistenceError):
            persist_state.persist(self.repo)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), before)
        self.assertTrue((self.repo / persist_state.RECOVERY_PATH).exists())

    def test_genuine_first_run_can_bootstrap_absent_remote_state(self):
        self.git(self.seed, 'rm', persist_state.STATE_PATH)
        self.git(self.seed, 'commit', '-m', 'first-run starting point')
        self.git(self.seed, 'push', 'origin', 'main')
        fresh = Path(self.temp) / 'fresh'
        self.git(Path(self.temp), 'clone', str(self.remote), str(fresh))
        self.configure(fresh)
        self.write_state(fresh, ['first-success'])
        self.assertEqual(persist_state.persist(fresh), 'persisted')
        self.assertEqual(self.remote_state()['sent_guids'], ['first-success'])

    def test_preserves_metadata_and_latest_timestamp_with_timezone(self):
        remote = {'sent_guids': ['a'], 'last_checked': '2026-10-02T14:00:00+09:00', 'remote_only': 'keep'}
        local = {'sent_guids': ['b'], 'last_checked': '2026-10-02T04:30:00+00:00', 'local_only': 'keep'}
        merged = persist_state.merge_states(remote, local)
        self.assertEqual(merged['sent_guids'], ['a', 'b'])
        self.assertEqual(merged['last_checked'], remote['last_checked'])
        self.assertEqual(merged['remote_only'], 'keep')
        self.assertEqual(merged['local_only'], 'keep')

    def test_conflicting_unknown_metadata_is_not_silently_lost(self):
        remote = {'sent_guids': [], 'last_checked': None, 'pending_guids': ['a']}
        local = {'sent_guids': [], 'last_checked': None, 'pending_guids': ['b']}
        with self.assertRaises(persist_state.PersistenceError):
            persist_state.merge_states(remote, local)

    def test_git_failure_logs_are_redacted(self):
        failure = subprocess.CalledProcessError(1, ['git'], stderr='https://secret-token@example.invalid')
        with patch.object(persist_state.subprocess, 'run', side_effect=failure), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(persist_state.PersistenceError) as raised:
                persist_state.git(self.repo, 'fetch')
        self.assertEqual(str(raised.exception), 'git_operation_failed')
        self.assertNotIn('secret-token', out.getvalue() + err.getvalue())


class WorkflowContractTests(unittest.TestCase):
    def test_partial_failure_persistence_does_not_hide_job_failure(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/check-rss.yml').read_text()
        self.assertIn("steps.notify.outcome == 'failure'", workflow)
        self.assertIn('always()', workflow)
        self.assertNotIn('continue-on-error', workflow)
        self.assertNotIn('git push --force', workflow)
        self.assertIn("cron: '23 * * * *'", workflow)
        self.assertIn('ref: main', workflow)
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertNotIn('  push:', workflow)
        self.assertNotIn('  pull_request:', workflow)
        self.assertIn('path: recovery/sent_articles.json', workflow)
        self.assertNotIn('actions: write', workflow)
