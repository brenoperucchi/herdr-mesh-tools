"""Reproduce ryzen9 and buffer-only reply failures without production agents."""
import contextlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from _herdr_message_ledger import Ledger, MessageError
from _herdr_message_worker import Worker, healthy
from test_message_ledger import endpoint


class FakeRuntime:
    def __init__(self):
        self.agents = {endpoint(n)['pane_id']: {**endpoint(n), 'agent_status': 'idle'} for n in ('a', 'b')}
        self.calls = []
        self.outcome = ('submitted', None)

    def effective(self, info):
        return info['agent_status']

    def status_command(self, directory, request_id):
        return f'herdr-message status {request_id}'

    def reply_command(self, directory, request_id):
        return f'herdr-message reply {request_id} --key reply:{request_id}'

    def get(self, pane):
        return self.agents[pane]

    @contextlib.contextmanager
    def reservation(self, dest):
        yield

    def prepare(self, dest):
        info = self.get(dest['pane_id'])
        if info['agent_session'] != dest['agent_session']:
            return False, 'endpoint_changed', info
        if info['agent_status'] != 'idle':
            return False, 'origin_busy', info
        return True, None, info

    def submit(self, dest, wire):
        self.calls.append((dest['pane_id'], wire))
        return self.outcome


@pytest.fixture
def world(tmp_path):
    with Ledger(tmp_path / 'state', 'test') as db:
        rt = FakeRuntime()
        clock = [100]
        w = Worker(db, rt, clock=lambda: clock[0], grace=10)
        yield db, rt, clock, w


def send(db, **kw):
    return db.send(endpoint('a'), endpoint('b'), 'Build', 'Build ryzen9', 'build', now=100, **kw)


def test_ryzen9_reply_waits_for_origin_without_resending_task(world):
    db, rt, clock, w = world
    req = send(db)
    w.tick()
    rt.agents[endpoint('a')['pane_id']]['agent_status'] = 'working'
    reply = db.reply(req['id'], endpoint('b'), 'Build complete', 'result', now=101)
    clock[0] = 102
    w.tick()
    assert db.status(req['id'])['state'] == 'return_pending_origin_busy'
    assert len(rt.calls) == 1
    assert db.body(reply['id']) == 'Build complete'
    rt.agents[endpoint('a')['pane_id']]['agent_status'] = 'idle'
    w.tick()
    w.tick()
    assert db.status(req['id'])['state'] == 'reply_submitted'
    assert len(rt.calls) == 2
    assert 'Build complete' in rt.calls[1][1]
    assert 'Build ryzen9' not in rt.calls[1][1]


def test_unknown_never_retries_and_creates_notice(world):
    db, rt, clock, w = world
    req = send(db)
    rt.outcome = ('unknown', 'timeout')
    w.tick()
    w.tick()
    assert db.get(req['id'])['transport'] == 'unknown'
    assert sum('Build ryzen9' in wire for _, wire in rt.calls) == 1
    notices = db.db.execute("SELECT count(*) FROM messages WHERE kind='notice'").fetchone()[0]
    assert notices == 1


def test_buffer_only_reply_gets_one_reminder_then_failure(world):
    db, rt, clock, w = world
    req = send(db, reply_timeout=30)
    w.tick()  # writing arbitrary output to a pane would not update the ledger.
    clock[0] = 111
    w.tick()
    clock[0] = 112
    w.tick()
    assert db.db.execute("SELECT count(*) FROM messages WHERE kind='reminder'").fetchone()[0] == 1
    clock[0] = 131
    w.tick()
    assert db.status(req['id'])['state'] == 'failed'
    assert db.db.execute("SELECT count(*) FROM messages WHERE kind='notice'").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM messages WHERE kind='reply'").fetchone()[0] == 0
    assert sum('Build ryzen9' in wire for _, wire in rt.calls) == 1


def test_crash_after_actual_submit_becomes_unknown(world):
    db, rt, clock, w = world
    req = send(db)
    def crash(dest, wire):
        rt.calls.append((dest['pane_id'], wire))
        raise SystemExit('power loss before receipt commit')
    rt.submit = crash
    with pytest.raises(SystemExit):
        w.tick()
    assert db.get(req['id'])['transport'] == 'in_flight'
    db.recover(now=101)
    rt.submit = FakeRuntime.submit.__get__(rt)
    w.tick()
    assert db.get(req['id'])['transport'] == 'unknown'
    assert sum('Build ryzen9' in wire for _, wire in rt.calls) == 1


def test_never_interrupts_legacy_sleep_or_writes_while_working(world):
    db, rt, clock, w = world
    req = send(db)
    rt.agents[endpoint('b')['pane_id']]['agent_status'] = 'working'
    w.tick()
    assert rt.calls == []
    assert db.get(req['id'])['transport'] == 'deferred'
    # No interrupt/kill/send-keys operation even exists in the injected transport.


def test_replaced_origin_never_gets_old_reply(world):
    db, rt, clock, w = world
    req = send(db)
    w.tick()
    db.reply(req['id'], endpoint('b'), 'Ready', 'result', now=101)
    rt.agents[endpoint('a')['pane_id']]['agent_session'] = dict(kind='id', source='herdr:codex', value='new')
    w.tick()
    assert db.status(req['id'])['state'] == 'return_failed'
    assert len(rt.calls) == 1


def test_no_worker_heartbeat_is_not_supervision(world):
    db, rt, clock, w = world
    assert not healthy(db, now=100)
    w.run(once=True)
    assert not healthy(db, now=100)  # oneshot has already released ownership.


def test_monitor_never_queues_reminder_after_reply(world):
    db, rt, clock, w = world
    req = send(db)
    w.tick()
    db.reply(req['id'], endpoint('b'), 'Done', 'result', now=101)
    rt.agents[endpoint('a')['pane_id']]['agent_status'] = 'working'
    clock[0] = 200
    w.tick()
    assert db.db.execute("SELECT count(*) FROM messages WHERE kind='reminder'").fetchone()[0] == 0
    assert db.status(req['id'])['state'] == 'return_pending_origin_busy'


def test_busy_old_messages_do_not_starve_ready_return(world):
    db, rt, clock, w = world
    for i in range(51):
        db.send(endpoint('a'), endpoint('b'), 'Notice', 'Info', f'n{i}', now=100, requires_reply=False)
    req = db.send(endpoint('a'), endpoint('b'), 'Work', 'Work', 'work', now=100)
    reply = db.reply(req['id'], endpoint('b'), 'Ready', 'result', now=101)
    rt.agents[endpoint('b')['pane_id']]['agent_status'] = 'working'
    clock[0] = 200
    w.tick()
    clock[0] = 201
    w.tick()
    assert db.get(reply['id'])['transport'] == 'submitted'


def test_long_busy_origin_retains_reply_beyond_old_fixed_timeout(world):
    db, rt, clock, w = world
    req = send(db, reply_timeout=3600)
    w.tick()
    reply = db.reply(req['id'], endpoint('b'), 'Build complete', 'result', now=101)
    rt.agents[endpoint('a')['pane_id']]['agent_status'] = 'working'
    clock[0] = 5000
    w.tick()
    assert db.status(req['id'])['state'] == 'return_pending_origin_busy'
    rt.agents[endpoint('a')['pane_id']]['agent_status'] = 'idle'
    clock[0] = 5001
    w.tick()
    assert db.get(reply['id'])['transport'] == 'submitted'
    assert len(rt.calls) == 2


def test_effective_idle_allows_buffer_only_reminder(world):
    db, rt, clock, w = world
    req = send(db)
    w.tick()
    rt.agents[endpoint('b')['pane_id']]['agent_status'] = 'unknown'
    rt.effective = lambda info: 'idle' if info['agent_status'] == 'unknown' else info['agent_status']
    clock[0] = 111
    w.tick()
    assert db.db.execute("SELECT count(*) FROM messages WHERE kind='reminder'").fetchone()[0] == 1


def test_already_notified_history_does_not_take_write_transactions(world):
    db, rt, clock, w = world
    for i in range(100):
        req = db.send(endpoint('a'), endpoint('b'), 'Work', 'Work', f'k{i}', now=100)
        token = db.begin(req['id'], now=101)
        db.finish(req['id'], token, 'unknown', now=102)
        w.notice(req['id'], 'Delivery unknown')
    statements = []
    db.db.set_trace_callback(statements.append)
    w.monitor()
    assert not any('BEGIN' in sql.upper() for sql in statements)
    db.db.set_trace_callback(None)


def test_active_oneshot_is_not_service_health(world):
    from _herdr_message_runtime import file_lock
    from _herdr_message_worker import owner
    import time
    db, rt, clock, w = world
    with file_lock(db.directory/'worker.lock'):
        db.heartbeat({**owner(), 'mode': 'once'}, time.time())
        assert not healthy(db)
        db.heartbeat({**owner(), 'mode': 'service'}, time.time())
        assert healthy(db)


@pytest.mark.parametrize('rc, payload', [(0, {'result': {'type': 'agent_prompted', 'agent': None}}), (1, {'error': {'code': []}})])
def test_malformed_receipt_persists_unknown_and_tick_continues(world, monkeypatch, rc, payload):
    import json
    import subprocess
    import _herdr_message_runtime as module
    from unittest.mock import Mock
    db, rt, clock, w = world
    req = send(db)
    second = db.send(endpoint('a'), endpoint('b'), 'Notice', 'Other', 'other', now=101, requires_reply=False)
    transport = module.Runtime(env={'HERDR_SOCKET_PATH': '/test/never-connect'})
    malformed = json.dumps(payload)
    run = Mock(return_value=subprocess.CompletedProcess([], rc, malformed if rc == 0 else '', malformed if rc else ''))
    monkeypatch.setattr(module.subprocess, 'run', run)
    rt.submit = transport.submit
    w.tick()
    assert db.get(req['id'])['transport'] == 'unknown'
    assert db.get(second['id'])['transport'] == 'unknown'
    assert run.call_count == 2


@pytest.mark.parametrize('error,expected', [('agent_not_found', 'return_failed'), ('offline', 'reply_recorded')])
def test_real_prepare_distinguishes_missing_origin_from_transient_outage(world, monkeypatch, error, expected):
    import json
    import subprocess
    import _herdr_message_runtime as module
    from unittest.mock import Mock
    db, rt, clock, w = world
    req = send(db)
    w.tick()
    reply = db.reply(req['id'], endpoint('b'), 'Saved forever', 'r', now=101)
    transport = module.Runtime(env={'HERDR_SOCKET_PATH': '/test/never-connect'})
    payload = json.dumps({'error': {'code': error}}) if error != 'offline' else 'connection lost'
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 1, '', payload)))
    rt.prepare = transport.prepare
    w.tick()
    assert db.status(req['id'])['state'] == expected
    assert db.body(reply['id']) == 'Saved forever'
    if error == 'agent_not_found':
        assert reply['id'] not in db.pending()
        assert db.db.execute("SELECT count(*) FROM messages WHERE kind='notice'").fetchone()[0] == 1


def test_monitor_notifies_proven_missing_recipient_before_deadline(world):
    db, rt, clock, w = world
    req = send(db)
    w.tick()
    rt.get = lambda pane: (_ for _ in ()).throw(MessageError('endpoint_missing'))
    clock[0] = 101
    w.monitor()
    assert db.get(req['id'])['response'] == 'failed'
    assert db.get(req['id'])['reason'] == 'recipient_changed'
