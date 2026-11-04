"""Read goal state only. A snapshot is neither caller proof nor compare-and-set.

The caller must independently bind the effective config root and session to the
current destination. No root discovery, UI probing, writes, or goal controls.
Live WAL databases are deliberately unknown until safe snapshot support exists.
Unknown state must never be interpreted as permission to create a goal.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

MAX_SOURCE_BYTES = 32 * 1024 * 1024
CODEX_COLUMNS = frozenset(('thread_id', 'goal_id', 'objective', 'status', 'token_budget',
                          'tokens_used', 'time_used_seconds', 'created_at_ms', 'updated_at_ms'))


class Uncertain(Exception):
    pass


def _result(status='unknown', *, reason=None, native_status=None, goal_id=None, condition_hash=None):
    return dict(status=status, reason=reason, native_status=native_status,
                goal_id=goal_id, condition_sha256=condition_hash)


def _uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _fingerprint(path):
    s = path.lstat()
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1
            or s.st_size > MAX_SOURCE_BYTES):
        raise Uncertain('unsafe_or_oversized_source')
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def _sqlite_fingerprints(path):
    result = [_fingerprint(path)]
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            result.append(_fingerprint(Path(str(path) + suffix)))
        except FileNotFoundError:
            result.append(None)
    return result


def _read_snapshot(path, before):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
            raise Uncertain('source_changed')
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES or _fingerprint(path) != before:
        raise Uncertain('source_changed_or_oversized')
    return raw


def _codex(root, sid):
    path = root / 'goals_1.sqlite'
    before = _sqlite_fingerprints(path)
    # Never pass the live path to SQLite's VFS: even mode=ro may create WAL
    # or modify shared-memory sidecars. Consult a bounded image in RAM only.
    # WAL/rollback recovery requires a separate proven snapshot mechanism.
    if any(sidecar is not None for sidecar in before[1:]):
        raise Uncertain('sqlite_sidecars_require_snapshot')
    raw = _read_snapshot(path, before[0])
    if (len(raw) < 100 or raw[:16] != b'SQLite format 3\0'
            or raw[18:20] != b'\x01\x01'):
        raise Uncertain('wal_or_unsupported_sqlite_snapshot')
    if _sqlite_fingerprints(path) != before:
        raise Uncertain('source_changed')
    db = sqlite3.connect(':memory:')
    try:
        if not hasattr(db, 'deserialize'):
            raise Uncertain('memory_snapshot_unsupported')
        db.deserialize(raw)
        db.execute('PRAGMA temp_store=MEMORY')
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        kind = db.execute("SELECT type FROM sqlite_master WHERE name='thread_goals'").fetchone()
        if kind != ('table',):
            raise Uncertain('unsupported_schema')
        columns = {r[1] for r in db.execute('PRAGMA table_info(thread_goals)')}
        if not CODEX_COLUMNS.issubset(columns):
            raise Uncertain('unsupported_schema')
        rows = db.execute('''SELECT goal_id,objective,status FROM thread_goals
            WHERE thread_id=? LIMIT 2''', (sid,)).fetchall()
        db.execute('COMMIT')
    finally:
        db.close()
    if _sqlite_fingerprints(path) != before:
        raise Uncertain('source_changed')
    if not rows:
        return _result('clear', reason='no_goal_for_bound_session', native_status='absent')
    if len(rows) != 1:
        raise Uncertain('ambiguous_goal')
    gid, objective, native = rows[0]
    if not _uuid(gid) or not isinstance(objective, str) or not objective:
        raise Uncertain('malformed_goal')
    if native not in ('active', 'paused', 'blocked', 'complete'):
        raise Uncertain('unsupported_goal_status')
    return _result('clear' if native == 'complete' else 'protected', native_status=native,
                   goal_id=gid, condition_hash=hashlib.sha256(objective.encode()).hexdigest())


def _claude(root, sid):
    # Discover only filenames for this exact UUID, never the newest transcript.
    paths = list((root / 'projects').glob('*/' + sid + '.jsonl'))
    if len(paths) != 1:
        raise Uncertain('transcript_missing_or_ambiguous')
    path = paths[0]
    if not path.resolve().is_relative_to(root):
        raise Uncertain('source_outside_root')
    before = _fingerprint(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != before:
            raise Uncertain('source_changed')
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if not raw or not raw.endswith(b'\n') or len(raw) > MAX_SOURCE_BYTES:
        raise Uncertain('incomplete_or_oversized_transcript')
    last = None
    for line in raw.split(b'\n'):
        if not line:
            continue
        record = json.loads(line.decode('utf-8'))
        if not isinstance(record, dict) or not isinstance(record.get('type'), str):
            raise Uncertain('malformed_transcript')
        if record.get('sessionId', sid) != sid:
            raise Uncertain('session_mismatch')
        attachment = record.get('attachment')
        if record['type'] != 'attachment':
            if 'goal' in record['type'].casefold():
                raise Uncertain('unsupported_goal_event')
            continue
        if not isinstance(attachment, dict) or not isinstance(attachment.get('type'), str):
            raise Uncertain('malformed_attachment')
        if attachment['type'] != 'goal_status':
            if 'goal' in attachment['type'].casefold():
                raise Uncertain('unsupported_goal_event')
            continue
        if (type(attachment.get('met')) is not bool
                or not isinstance(attachment.get('condition'), str) or not attachment['condition']
                or ('sentinel' in attachment and type(attachment['sentinel']) is not bool)
                or ('reason' in attachment and not isinstance(attachment['reason'], str))):
            raise Uncertain('malformed_goal_status')
        last = attachment
    if _fingerprint(path) != before:
        raise Uncertain('source_changed')
    if last is None:
        raise Uncertain('no_goal_history')
    return _result('clear' if last['met'] else 'protected',
                   native_status='met_or_cleared' if last['met'] else 'not_met',
                   condition_hash=hashlib.sha256(last['condition'].encode()).hexdigest())


def read_goal(kind, root, session_id, *, binding_verified=False):
    """Return protected/clear/unknown, never authorization to overwrite a goal.

    `binding_verified` is a required assertion by the internal orchestrator that
    independently resolved the current target, session and effective config
    root. This module cannot establish that binding from files or UUID alone.
    Even clear is a point-in-time observation with an external-input race.
    """
    try:
        if binding_verified is not True:
            raise Uncertain('session_binding_unproven')
        if not _uuid(session_id):
            raise Uncertain('invalid_session_id')
        root = Path(root)
        if not root.is_absolute():
            raise Uncertain('invalid_config_root')
        root = root.resolve(strict=True)
        if not root.is_dir() or root.stat().st_uid != os.getuid():
            raise Uncertain('invalid_config_root')
        if kind == 'codex':
            return _codex(root, session_id)
        if kind == 'claude':
            return _claude(root, session_id)
        raise Uncertain('unsupported_kind')
    except Uncertain as exc:
        return _result(reason=str(exc))
    except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError):
        # No path, text from a transcript or raw database error escapes here.
        return _result(reason='source_unreadable_or_invalid')
