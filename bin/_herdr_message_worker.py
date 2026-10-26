"""Supervisable reconciler; one owner per ledger, bounded I/O, no agent-side wait."""
import json
import os
import signal
import time

from _herdr_message_ledger import MessageError, same_endpoint
from _herdr_message_runtime import envelope, file_lock


def owner():
    raw = open(f'/proc/{os.getpid()}/stat').read()
    return dict(pid=os.getpid(), start=raw[raw.rfind(')') + 2:].split()[19])


def healthy(db, now=None):
    now = time.time() if now is None else now
    try:
        h = json.loads(db.meta('heartbeat') or '{}')
        if not isinstance(h, dict) or not isinstance(h.get('owner'), dict) or h['owner'].get('mode') != 'service':
            return False
        if not -2 <= now - h['time'] < 120:
            return False
        raw = open(f'/proc/{int(h["owner"]["pid"])}/stat').read()
        if raw[raw.rfind(')') + 2:].split()[19] != h['owner']['start']:
            return False
        try:
            with file_lock(db.directory / 'worker.lock'):
                return False  # heartbeat without an active exclusive owner is not supervision.
        except MessageError as exc:
            return str(exc) == 'reservation_busy'
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


class Worker:
    def __init__(self, db, runtime, *, clock=time.time, grace=20):
        self.db, self.runtime, self.clock, self.grace = db, runtime, clock, grace
        self.mode = 'unmanaged'
        db.db.execute('CREATE INDEX IF NOT EXISTS monitor_messages ON messages(request_id,kind)')

    def monitor(self):
        db, now = self.db, self.clock()
        ids = [r[0] for r in db.db.execute("""SELECT q.id FROM messages q
            WHERE q.kind='request' AND q.requires_reply=1 AND (
                (q.transport='submitted' AND q.response='awaiting_reply') OR (
                    NOT EXISTS (SELECT 1 FROM messages n WHERE n.request_id=q.id AND n.kind='notice')
                    AND ((q.transport IN ('unknown','failed') AND q.response NOT IN ('reply_recorded','reply_submitted'))
                        OR EXISTS (SELECT 1 FROM messages r WHERE r.request_id=q.id AND r.kind='reply' AND r.transport IN ('unknown','failed')))))""")]
        for mid in ids:
            req = db.get(mid)
            if req['response'] in ('reply_recorded', 'reply_submitted'):
                row = db.db.execute("SELECT id FROM messages WHERE request_id=? AND kind='reply'", (mid,)).fetchone()
                if row and db.get(row[0])['transport'] in ('unknown', 'failed'):
                    self.notice(mid, f'Pedido {mid}: resposta registrada, mas entrega de retorno falhou ou é incerta. Consulte status; não reenvie a tarefa.')
                continue
            if req['transport'] == 'unknown':
                self.notice(mid, 'Resultado de envio desconhecido; a tarefa não será reenviada automaticamente.')
                continue
            if req['transport'] == 'failed':
                self.notice(mid, 'Falha de entrega do pedido; consulte herdr-message status ' + mid)
                continue
            if req['transport'] != 'submitted':
                continue
            if req['response'] == 'failed':
                continue
            try:
                info = self.runtime.get(req['destination']['pane_id'])
                changed = not same_endpoint(req['destination'], info)
            except MessageError as exc:
                info, changed = None, str(exc) == 'endpoint_missing'  # server outage does not prove a replaced endpoint.
            if changed or now >= req['reply_deadline']:
                with db.transaction():
                    # reply may have been committed since the observation.
                    fresh = db.get(mid)
                    if fresh['response'] in ('reply_recorded', 'reply_submitted', 'failed'):
                        continue
                    reason = 'recipient_changed' if changed else 'reply_deadline_exceeded'
                    db.db.execute("UPDATE messages SET response='failed',reason=?,updated=? WHERE id=?",
                                  (reason, now, mid))
                    db.event(mid, 'response_failed', now, reason)
                    self._notice(mid, f'Pedido {mid}: {reason}. Não houve retorno registrado no prazo; '
                                 'isso não prova que a tarefa não tenha sido executada.', now)
                continue
            if not info or self.runtime.effective(info) not in ('idle', 'done'):
                with db.transaction():
                    db.db.execute('UPDATE messages SET settled_since=NULL WHERE id=?', (mid,))
                continue
            with db.transaction():
                fresh = db.get(mid)
                if fresh['response'] != 'awaiting_reply' or fresh['reminder']:
                    continue
                if fresh['settled_since'] is None:
                    db.db.execute('UPDATE messages SET settled_since=? WHERE id=?', (now, mid))
                elif now - fresh['settled_since'] >= self.grace:
                    db.system_message(mid, 'reminder', f'Pedido {mid} continua sem retorno registrado. '
                                      f'Use {self.runtime.reply_command(db.directory, mid)} --file /caminho/absoluto '
                                      'para registrar resposta ou impossibilidade. Não repita a tarefa original.', now)
                    db.db.execute('UPDATE messages SET reminder=1 WHERE id=?', (mid,))
                    db.event(mid, 'reminder_queued', now)

    def _notice(self, mid, body, now):
        exists = self.db.db.execute("SELECT 1 FROM messages WHERE request_id=? AND kind='notice'", (mid,)).fetchone()
        if not exists:
            self.db.system_message(mid, 'notice', body + '\nEstado atual: ' + self.runtime.status_command(self.db.directory, mid), now)
            self.db.event(mid, 'notice_queued', now)

    def notice(self, mid, body):
        if self.db.db.execute("SELECT 1 FROM messages WHERE request_id=? AND kind='notice'", (mid,)).fetchone():
            return
        with self.db.transaction():
            self._notice(mid, body, self.clock())

    def deliver(self, mid):
        db, now = self.db, self.clock()
        message = db.get(mid)
        if message['transport'] not in ('queued', 'deferred'):
            return
        if message['kind'] not in ('reply', 'notice') and now >= message['send_deadline']:
            db.fail(mid, 'delivery_deadline_exceeded', now=now)
            return
        if message['kind'] == 'reminder':
            req = db.get(message['request_id'])
            if req['response'] != 'awaiting_reply':
                db.fail(mid, 'reminder_superseded', now=now)
                return
        if message['kind'] == 'request' and message['requires_reply']:
            other = db.db.execute("""SELECT 1 FROM messages WHERE destination=? AND id!=?
                AND kind='request' AND transport='submitted' AND response='awaiting_reply' LIMIT 1""",
                                  (json.dumps(message['destination'], sort_keys=True, separators=(',', ':'), ensure_ascii=False), mid)).fetchone()
            if other:
                db.defer(mid, 'recipient_has_open_request', now=now)
                return
        try:
            with self.runtime.reservation(message['destination']):
                allowed, reason, _ = self.runtime.prepare(message['destination'])
                if not allowed:
                    if reason == 'endpoint_changed':
                        db.fail(mid, reason, now=now)
                    else:
                        if message['kind'] != 'reply' and reason == 'origin_busy':
                            reason = 'target_busy'
                        db.defer(mid, reason, now=now)
                    return
                token = db.begin(mid, now=self.clock())
                # Intent is committed before the only externally effectful call.
                reply_command = self.runtime.reply_command(db.directory, mid) if message['kind'] == 'request' and message['requires_reply'] else None
                outcome, detail = self.runtime.submit(message['destination'], envelope(message, reply_command))
                db.finish(mid, token, outcome, now=self.clock(), reason=detail)
        except MessageError:
            # If intent exists, preserving in_flight is deliberate: next exclusive
            # owner recovers it as unknown. Never roll an interrupted send back to queued.
            if db.get(mid)['transport'] in ('queued', 'deferred'):
                db.defer(mid, 'reservation_or_observation_unavailable', now=now)

    def tick(self):
        self.db.heartbeat({**owner(), 'mode': self.mode}, self.clock())
        self.monitor()
        batch = [row[0] for row in self.db.db.execute("SELECT id FROM messages WHERE transport IN ('queued','deferred') ORDER BY updated,created,id LIMIT 50")]
        for mid in batch:
            self.db.heartbeat({**owner(), 'mode': self.mode}, self.clock())
            self.deliver(mid)
        # Detect newly unknown/failed requests without waiting another full tick.
        self.monitor()
        self.db.heartbeat({**owner(), 'mode': self.mode}, self.clock())

    def run(self, *, once=False, interval=2):
        stop = False
        def stopping(signum, frame):
            nonlocal stop
            stop = True  # never send signals or terminal input to a recipient.
        old = {}
        self.mode = 'once' if once else 'service'
        with file_lock(self.db.directory / 'worker.lock'):
            self.db.recover(now=self.clock())
            if not once:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    old[sig] = signal.signal(sig, stopping)
            try:
                while True:
                    self.tick()
                    if once or stop:
                        break
                    time.sleep(interval)  # supervised service only, never a caller-side delivery loop.
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
