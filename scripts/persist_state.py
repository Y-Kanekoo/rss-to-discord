"""Persist checkpointed sends with bounded, non-force pushes of state only.

The original checkout is never reset. Worktrees start at the latest remote main,
so concurrent code changes survive. An unrecoverable failure leaves a validated
recovery snapshot for the existing workflow's artifact step.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from check_rss import load_state, save_state, validate_state

STATE_PATH = 'data/sent_articles.json'
RECOVERY_PATH = 'recovery/sent_articles.json'
MAX_ATTEMPTS = 3


class PersistenceError(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    # Capture output because remote URLs and authentication failures can contain
    # credentials. Only fixed error codes are printed by main().
    try:
        result = subprocess.run(['git', '-C', str(root), *args], check=True,
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except (subprocess.SubprocessError, OSError) as error:
        raise PersistenceError('git_operation_failed') from error
    return result.stdout.strip()


def merge_states(remote: dict, local: dict) -> dict:
    validate_state(remote)
    validate_state(local)
    merged = dict(remote)
    for key, value in local.items():
        if key in ('sent_guids', 'last_checked'):
            continue
        if key in remote and remote[key] != value:
            raise PersistenceError('state_metadata_conflict')
        merged[key] = value
    merged['sent_guids'] = sorted(set(remote['sent_guids']) | set(local['sent_guids']))
    checked = [s['last_checked'] for s in (remote, local) if s['last_checked'] is not None]
    try:
        def timestamp(value):
            parsed = datetime.fromisoformat(value)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        merged['last_checked'] = max(checked, key=timestamp) if checked else None
    except ValueError as error:
        raise PersistenceError('state_timestamp_invalid') from error
    return merged


def persist(root: Path) -> str:
    # A missing local file may mean the sender failed before creating anything.
    # It must not create/reset remote history.
    local_path = root / STATE_PATH
    if not local_path.is_file():
        return 'no_local_state'
    local = load_state(str(local_path))
    last_error = 'state_push_failed'
    for _ in range(MAX_ATTEMPTS):
        worktree = None
        with tempfile.TemporaryDirectory(prefix='rss-state-') as temp:
            try:
                git(root, 'fetch', '--no-tags', 'origin', 'refs/heads/main')
                remote_sha = git(root, 'rev-parse', 'FETCH_HEAD')
                worktree = Path(temp) / 'checkout'
                git(root, 'worktree', 'add', '--detach', str(worktree), remote_sha)
                remote_path = worktree / STATE_PATH
                if not remote_path.is_file():
                    # Bootstrap only if the sender's original checkout also had
                    # no committed state. A concurrent deletion is not a reset.
                    if git(root, 'ls-tree', 'HEAD', '--', STATE_PATH):
                        raise PersistenceError('remote_state_missing')
                    remote = {'sent_guids': [], 'last_checked': None}
                else:
                    remote = load_state(str(remote_path))
                merged = merge_states(remote, local)
                # A retry can discover that a previous ambiguous push succeeded.
                if merged == remote:
                    return 'already_persisted'
                save_state(str(remote_path), merged)
                git(worktree, 'add', '--', STATE_PATH)
                git(worktree, 'commit', '-m', '[chore] 送信済み記事の状態を更新')
                git(worktree, 'push', 'origin', 'HEAD:refs/heads/main')
                return 'persisted'
            except Exception as error:
                # Never print raw exceptions, including arbitrary subprocess text.
                last_error = str(error) if isinstance(error, PersistenceError) else 'state_persistence_failed'
                if last_error not in ('git_operation_failed', 'state_push_failed'):
                    break
            finally:
                if worktree is not None:
                    try:
                        git(root, 'worktree', 'remove', '--force', str(worktree))
                    except PersistenceError:
                        pass
    # Only validated state is retained; no environment, logs or source tree.
    save_state(str(root / RECOVERY_PATH), local)
    raise PersistenceError(last_error)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        status = persist(root)
    except Exception:
        print(json.dumps({'status': 'failed', 'error': 'state_persistence_failed',
                          'recovery_available': (root / RECOVERY_PATH).is_file()}))
        return 1
    print(json.dumps({'status': status}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
