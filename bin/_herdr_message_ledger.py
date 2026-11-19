"""Private durable outbox. No process, terminal or shell I/O in this module."""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager


class MessageError(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def endpoint(info):
    """Stable incarnation, never a lifecycle sequence or caller-supplied signature."""
    keys = ('name', 'agent', 'pane_id', 'workspace_id', 'terminal_id', 'cwd')
    result = {k: info.get(k) for k in keys}
    session = info.get('agent_session')
    if not all(isinstance(v, str) and v for v in result.values()):
        raise MessageError('identity_unproven: incomplete endpoint')
    if not isinstance(session, dict) or not all(isinstance(session.get(k), str) and session[k]
                                              for k in ('kind', 'source', 'value')):
        raise MessageError('identity_unproven: session missing')
    result['agent_session'] = {k: session[k] for k in ('kind', 'source', 'value')}
    return result


def same_endpoint(a, b):
    return endpoint(a) == endpoint(b)


def positive(value):
    if not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise MessageError('invalid positive duration')
    return value


# Prazo de entrega: pedido com resposta vence em 15 min e o remetente é avisado;
# --notice (sem resposta) espera 24 h, o bastante para um pane parado numa caixa de
# diálogo por horas, e ao vencer o remetente também é avisado (worker.monitor).
REQUEST_SEND_TIMEOUT = 900
NOTICE_SEND_TIMEOUT = 24 * 3600


def human_duration(seconds):
    minutes = max(1, round(seconds / 60))
    if minutes < 120:
        return f'{minutes} min'
    hours, rest = divmod(minutes, 60)
    return f'{hours} h' + (f' {rest} min' if rest else '')


def text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value.encode()) > limit:
        raise MessageError(f'invalid {label}')
    return value


class Ledger:
    def __init__(self, directory, scope):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise MessageError('state directory must be owned, private and not a symlink')
        path = self.directory / 'ledger.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or st.st_mode & 0o077:
                raise MessageError('unsafe ledger file')
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=3, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA busy_timeout=3000')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY, request_id TEXT REFERENCES messages(id),
                    kind TEXT NOT NULL CHECK(kind IN ('request','reply','reminder','notice')),
                    sender TEXT NOT NULL, destination TEXT NOT NULL,
                    subject TEXT NOT NULL, body TEXT NOT NULL,
                    dedup TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
                    requires_reply INTEGER NOT NULL,
                    response TEXT NOT NULL,
                    transport TEXT NOT NULL CHECK(transport IN
                        ('queued','deferred','in_flight','submitted','unknown','failed')),
                    reason TEXT, attempt TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    send_deadline REAL NOT NULL, reply_timeout REAL NOT NULL,
                    reply_deadline REAL, submitted REAL, reminder INTEGER NOT NULL DEFAULT 0,
                    settled_since REAL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_reply ON messages(request_id) WHERE kind='reply';
                CREATE UNIQUE INDEX IF NOT EXISTS one_system_message ON messages(request_id,kind)
                    WHERE kind IN ('reminder','notice');
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
                    time REAL NOT NULL, event TEXT NOT NULL, detail TEXT);
            ''')
            with self.transaction():
                self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('scope', scope))
                if self.meta('scope') != scope:
                    raise MessageError('server_scope_mismatch')
                self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('schema', '1'))
                if self.meta('schema') != '1':
                    raise MessageError('unsupported ledger schema')
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def mark_failure_cutoff(self):
        """Remember the last event sequence at first activation: only failures recorded after it
        are announced. Ordered by the ledger's own sequence, not by the wall clock."""
        if self.meta('failure_notice_after_seq') is not None:
            return  # already set: no write transaction per tick
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO meta SELECT 'failure_notice_after_seq', "
                            "CAST(COALESCE(MAX(seq),0) AS TEXT) FROM events")

    def heartbeat(self, owner, now=None):
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                            ('heartbeat', encode(dict(owner=owner, time=time.time() if now is None else now))))

    def event(self, mid, event, now, detail=None):
        self.db.execute('INSERT INTO events(message_id,time,event,detail) VALUES (?,?,?,?)',
                        (mid, now, event, detail))

    def get(self, mid):
        row = self.db.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        if not row:
            raise MessageError('message_not_found')
        result = dict(row)
        for k in ('sender', 'destination'):
            result[k] = json.loads(result[k])
        return result

    def _insert(self, sender, destination, subject, body, key, now, send_timeout,
                reply_timeout, requires_reply, kind='request', request_id=None):
        sender, destination = endpoint(sender), endpoint(destination)
        text(subject, 300, 'subject')
        if '\n' in subject or '\r' in subject:
            raise MessageError('subject must be one line')
        text(body, 65536, 'body')
        text(key, 200, 'key')
        positive(send_timeout)
        positive(reply_timeout)
        if same_endpoint(sender, destination):
            raise MessageError('self_send_refused')
        dedup = hashlib.sha256(encode([sender, kind, key]).encode()).hexdigest()
        digest = hashlib.sha256(encode([destination, subject, body, kind, request_id,
                                       requires_reply, send_timeout, reply_timeout]).encode()).hexdigest()
        old = self.db.execute('SELECT id,digest FROM messages WHERE dedup=?', (dedup,)).fetchone()
        if old:
            if old['digest'] != digest:
                raise MessageError('idempotency_conflict')
            return self.get(old['id'])
        mid = str(uuid.uuid4())
        self.db.execute('''INSERT INTO messages(id,request_id,kind,sender,destination,subject,body,
            dedup,digest,requires_reply,response,transport,created,updated,send_deadline,reply_timeout)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (mid, request_id, kind, encode(sender), encode(destination), subject, body, dedup, digest,
             int(requires_reply), 'awaiting_submission' if requires_reply else 'not_required',
             'queued', now, now, now + send_timeout, reply_timeout))
        self.event(mid, 'queued', now)
        return self.get(mid)

    def send(self, sender, destination, subject, body, key, *, now=None,
             requires_reply=True, send_timeout=None, reply_timeout=900):
        now = time.time() if now is None else now
        if send_timeout is None:
            send_timeout = REQUEST_SEND_TIMEOUT if requires_reply else NOTICE_SEND_TIMEOUT
        with self.transaction():
            return self._insert(sender, destination, subject, body, key, now,
                                send_timeout, reply_timeout, requires_reply)

    def reply(self, request_id, sender, body, key, *, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            req = self.get(request_id)
            if req['kind'] != 'request' or not req['requires_reply']:
                raise MessageError('reply_not_required')
            if not same_endpoint(sender, req['destination']):
                raise MessageError('reply_identity_mismatch')
            previous = self.db.execute("SELECT id FROM messages WHERE request_id=? AND kind='reply'",
                                       (request_id,)).fetchone()
            # _insert validates an existing key and payload without creating a second final reply.
            dedup = hashlib.sha256(encode([endpoint(sender), 'reply', key]).encode()).hexdigest()
            if previous and not self.db.execute('SELECT 1 FROM messages WHERE dedup=? AND id=?',
                                               (dedup, previous[0])).fetchone():
                raise MessageError('final_reply_already_recorded')
            reply_subject = 'Re: ' + req['subject'].encode()[:296].decode('utf-8', 'ignore')
            result = self._insert(sender, req['sender'], reply_subject, body, key,
                                  now, 900, 900, False, 'reply', request_id)
            if previous:
                return result
            if req['transport'] in ('queued', 'deferred'):
                self.db.execute("UPDATE messages SET transport='failed',reason='superseded_by_reply',updated=? WHERE id=?", (now, request_id))
                self.event(request_id, 'superseded_by_reply', now)
            reminders = self.db.execute("SELECT id FROM messages WHERE request_id=? AND kind='reminder' AND transport IN ('queued','deferred')", (request_id,)).fetchall()
            for row in reminders:
                self.db.execute("UPDATE messages SET transport='failed',reason='superseded_by_reply',updated=? WHERE id=?", (now, row[0]))
                self.event(row[0], 'superseded_by_reply', now)
            self.db.execute("UPDATE messages SET response='reply_recorded',updated=? WHERE id=?",
                            (now, request_id))
            self.event(request_id, 'reply_recorded', now, result['id'])
            return result

    def system_message(self, request_id, kind, body, now):
        if not self.db.in_transaction:
            raise MessageError('system_message requires an open transaction')
        if kind not in ('notice', 'reminder'):
            raise MessageError('invalid system kind')
        req = self.get(request_id)
        sender, dest = (req['sender'], req['destination']) if kind == 'reminder' else (req['destination'], req['sender'])
        return self._insert(sender, dest, 'Conversation status', body, f'{kind}:{request_id}',
                            now, 900, 900, False, kind, request_id)

    def deferred_summary(self, viewer, observer, visible, *, now=None):
        """Per destination: how many messages wait for delivery and the age of the oldest.

        Metadata only (no sender, subject or body). The observer sees only the destinations
        in `visible`; any other proven caller sees only its own queue.
        """
        if not isinstance(viewer, dict) or not isinstance(viewer.get('name'), str):
            raise MessageError('identity_unproven: caller missing')
        now = time.time() if now is None else now
        rows = self.db.execute("""SELECT json_extract(destination,'$.name') AS name, COUNT(*) AS n, MIN(created) AS oldest
            FROM messages WHERE transport IN ('queued','deferred') GROUP BY name""").fetchall()
        result = [dict(destination=r['name'], deferred=r['n'], oldest_age_seconds=max(0, int(now - r['oldest'])))
                  for r in rows if r['name'] == viewer['name'] or (viewer['name'] == observer and r['name'] in visible)]
        return sorted(result, key=lambda r: (-r['oldest_age_seconds'], r['destination']))

    def delivery_batch(self, limit=50):
        """Oldest first within each destination, so a long queue is never delivered out of order.

        A request that cannot be submitted because another request to the same endpoint still
        awaits its reply (the `recipient_has_open_request` rule in Worker.deliver) goes after the
        messages that can: it must not hold the head of the queue ahead of a ready reply or notice.
        """
        return [r[0] for r in self.db.execute("""SELECT id FROM (
            SELECT id, updated, created, ROW_NUMBER() OVER (PARTITION BY destination
                ORDER BY blocked, created, id) AS position
            FROM (SELECT m.id, m.updated, m.created, m.destination,
                    (m.kind='request' AND m.requires_reply=1 AND EXISTS (
                        SELECT 1 FROM messages o WHERE o.destination=m.destination AND o.id!=m.id
                        AND o.kind='request' AND o.transport='submitted' AND o.response='awaiting_reply')) AS blocked
                  FROM messages m WHERE m.transport IN ('queued','deferred')))
            ORDER BY position, updated, created, id LIMIT ?""", (limit,))]

    def overdue_deliveries(self, now):
        return [r[0] for r in self.db.execute("""SELECT id FROM messages WHERE transport IN ('queued','deferred')
            AND kind NOT IN ('reply','notice') AND send_deadline <= ? ORDER BY created,id""", (now,))]

    def last_deferred_reason(self, mid):
        row = self.db.execute("SELECT detail FROM events WHERE message_id=? AND event='deferred' ORDER BY seq DESC LIMIT 1",
                              (mid,)).fetchone()
        return row[0] if row else None

    def pending(self):
        return [r[0] for r in self.db.execute("SELECT id FROM messages WHERE transport IN ('queued','deferred') ORDER BY created,id")]

    def begin(self, mid, *, now):
        with self.transaction():
            m = self.get(mid)
            if m['transport'] not in ('queued', 'deferred'):
                raise MessageError('submission_not_retryable')
            if m['kind'] == 'reminder' and self.get(m['request_id'])['response'] != 'awaiting_reply':
                raise MessageError('reminder_superseded')
            token = str(uuid.uuid4())
            self.db.execute("UPDATE messages SET transport='in_flight',attempt=?,reason=NULL,updated=? WHERE id=?",
                            (token, now, mid))
            self.event(mid, 'in_flight', now, token)
            return token

    def finish(self, mid, attempt, outcome, *, now, reason=None):
        if outcome not in ('submitted', 'unknown', 'deferred', 'failed'):
            raise MessageError('invalid submission outcome')
        with self.transaction():
            m = self.get(mid)
            if m['transport'] != 'in_flight' or m['attempt'] != attempt:
                raise MessageError('submission_attempt_mismatch')
            if outcome == 'deferred':
                conversation = self.get(m['request_id']) if m['kind'] == 'reminder' else m
                if m['kind'] in ('request', 'reminder') and conversation['response'] in ('reply_recorded', 'reply_submitted'):
                    outcome, reason = 'failed', 'superseded_by_reply'
            self.db.execute('UPDATE messages SET transport=?,reason=?,updated=? WHERE id=?',
                            (outcome, reason, now, mid))
            if outcome == 'submitted':
                self.db.execute('''UPDATE messages SET submitted=?,reply_deadline=?,
                    response=CASE WHEN requires_reply=1 AND response='awaiting_submission'
                        THEN 'awaiting_reply' ELSE response END WHERE id=?''',
                    (now, now + m['reply_timeout'], mid))
                if m['kind'] == 'reply':
                    self.db.execute("UPDATE messages SET response='reply_submitted',updated=? WHERE id=?",
                                    (now, m['request_id']))
            self.event(mid, outcome, now, reason)

    def defer(self, mid, reason, *, now, touch=True):
        """`touch=False` is for a refusal copied from the destination's cache in the same tick:
        nothing was observed for this message, so an unchanged deferral is not rewritten.
        A real observation always refreshes `updated`; that is what rotates destinations in
        delivery_batch when more of them are waiting than fit in a batch."""
        with self.transaction():
            before = self.get(mid)
            if not touch and before['transport'] == 'deferred' and before['reason'] == reason:
                return
            cursor = self.db.execute("UPDATE messages SET transport='deferred',reason=?,updated=? WHERE id=? AND transport IN ('queued','deferred')",
                                     (reason, now, mid))
            if cursor.rowcount != 1:
                raise MessageError('submission_not_retryable')
            if before['transport'] != 'deferred' or before['reason'] != reason:
                self.event(mid, 'deferred', now, reason)

    def fail(self, mid, reason, *, now):
        with self.transaction():
            cursor = self.db.execute("UPDATE messages SET transport='failed',reason=?,updated=? WHERE id=? AND transport IN ('queued','deferred')",
                                     (reason, now, mid))
            if cursor.rowcount != 1:
                raise MessageError('submission_not_retryable')
            self.event(mid, 'failed', now, reason)

    def recover(self, *, now):
        """Caller must hold the exclusive worker lock. Never retry orphaned intent."""
        with self.transaction():
            for row in self.db.execute("SELECT id FROM messages WHERE transport='in_flight'").fetchall():
                self.db.execute("UPDATE messages SET transport='unknown',reason='worker_interrupted',updated=? WHERE id=?",
                                (now, row[0]))
                self.event(row[0], 'unknown', now, 'worker_interrupted')

    def status(self, mid):
        m = self.get(mid)
        reply = self.db.execute("SELECT id FROM messages WHERE request_id=? AND kind='reply'", (mid,)).fetchone()
        r = self.get(reply[0]) if reply else None
        state = m['response'] if m['transport'] == 'submitted' else m['transport']
        if r:
            state = 'reply_submitted' if r['transport'] == 'submitted' else 'reply_recorded'
            if r['reason'] == 'origin_busy' and r['transport'] == 'deferred':
                state = 'return_pending_origin_busy'
            elif r['transport'] in ('unknown', 'failed'):
                state = 'return_' + r['transport']
        return dict(id=mid, state=state, transport=m['transport'], response=m['response'],
                    reason=m['reason'], sender=m['sender'], destination=m['destination'],
                    reply=None if not r else dict(id=r['id'], transport=r['transport'], reason=r['reason']),
                    created=m['created'], reply_deadline=m['reply_deadline'])

    def body(self, mid):
        return self.get(mid)['body']
