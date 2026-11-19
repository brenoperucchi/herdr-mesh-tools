"""A message that cannot be delivered must not fail silently, nor die in 15 minutes
when it is only a notice (2026-11-19: coordinator pane stuck 5 h in a dialog)."""
import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from _herdr_message_ledger import (Ledger, MessageError, NOTICE_SEND_TIMEOUT,
                                   REQUEST_SEND_TIMEOUT)
from _herdr_message_worker import Worker
from test_message_ledger import endpoint

path = Path(__file__).resolve().parents[1] / 'bin/herdr-message'
loader = SourceFileLoader('message_cli_delivery_failure', str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

HOUR = 3600


class Runtime:
    """a, b, c are agents; b starts busy (the recipient stuck behind a dialog)."""

    def __init__(self):
        self.agents = {endpoint(n)['pane_id']: {**endpoint(n), 'agent_status': 'idle'} for n in 'abc'}
        self.agents[endpoint('b')['pane_id']]['agent_status'] = 'working'
        self.prepared = []
        self.calls = []

    def status(self, name, value):
        self.agents[endpoint(name)['pane_id']]['agent_status'] = value

    def effective(self, info):
        return info['agent_status']

    def status_command(self, directory, request_id):
        return f'herdr-message status {request_id}'

    def reply_command(self, directory, request_id):
        return f'herdr-message reply {request_id}'

    def get(self, pane):
        return self.agents[pane]

    @contextlib.contextmanager
    def reservation(self, dest):
        yield

    def prepare(self, dest):
        self.prepared.append(dest['pane_id'])
        info = self.get(dest['pane_id'])
        if info['agent_session'] != dest['agent_session']:
            return False, 'endpoint_changed', info
        if info['agent_status'] != 'idle':
            return False, 'origin_busy', info
        return True, None, info

    def submit(self, dest, wire):
        self.calls.append((dest['pane_id'], wire))
        return 'submitted', None


@pytest.fixture
def world(tmp_path):
    with Ledger(tmp_path / 'state', 'test') as db:
        rt = Runtime()
        clock = [1000]
        yield db, rt, clock, Worker(db, rt, clock=lambda: clock[0], grace=10)


def notice(db, key, subject='Push feito', sender='a', to='b', now=1000, **kw):
    return db.send(endpoint(sender), endpoint(to), subject, 'corpo secreto', key,
                   now=now, requires_reply=False, **kw)


def state(db, mid):
    m = db.get(mid)
    return m['transport'], m['reason']


def failure_notices(db, request_id):
    return db.db.execute("SELECT id,transport,destination FROM messages WHERE request_id=? AND kind='notice'",
                         (request_id,)).fetchall()


# 1. Notice deferred past the old 900 s limit keeps waiting and is delivered in creation order.
def test_notice_stays_deferred_past_15_minutes_and_is_delivered_in_order(world):
    db, rt, clock, w = world
    first, second = notice(db, 'n1', 'Primeiro', now=1000), notice(db, 'n2', 'Segundo', now=1001)
    for now in (1010, 1000 + 901, 1000 + 2 * HOUR, 1000 + 5 * HOUR):
        clock[0] = now
        w.tick()
        assert state(db, first['id']) == ('deferred', 'target_busy')
        assert state(db, second['id']) == ('deferred', 'target_busy')
    rt.status('b', 'idle')
    clock[0] = 1000 + 5 * HOUR + 1
    w.tick()
    assert state(db, first['id']) == ('submitted', None)
    assert state(db, second['id']) == ('submitted', None)
    assert ['Primeiro' in wire for _, wire in rt.calls] == [True, False]
    assert 'Segundo' in rt.calls[1][1]


def test_default_deadlines_notice_24h_request_15min_and_explicit_value_wins(world):
    db, *_ = world
    assert db.get(notice(db, 'n')['id'])['send_deadline'] == 1000 + NOTICE_SEND_TIMEOUT == 1000 + 24 * HOUR
    assert db.get(notice(db, 'n-short', send_timeout=60)['id'])['send_deadline'] == 1060
    req = db.send(endpoint('a'), endpoint('b'), 'Pedido', 'x', 'r', now=1000)
    assert db.get(req['id'])['send_deadline'] == 1000 + REQUEST_SEND_TIMEOUT == 1900


# 2. Expired notice: failed, and the SENDER gets exactly one warning with destination and subject.
def test_expired_notice_warns_the_sender_once(world):
    db, rt, clock, w = world
    n = notice(db, 'n', 'Push feito')
    clock[0] = 1000 + 24 * HOUR
    w.tick()
    assert state(db, n['id']) == ('failed', 'delivery_deadline_exceeded')
    rt.status('a', 'idle')
    w.tick()
    w.tick()
    rows = failure_notices(db, n['id'])
    assert len(rows) == 1 and rows[0]['transport'] == 'submitted'
    assert json.loads(rows[0]['destination'])['name'] == 'a'
    pane, wire = [c for c in rt.calls if c[0] == endpoint('a')['pane_id']][0]
    assert n['id'] in wire and 'Push feito' in wire and 'para b' in wire
    assert 'NÃO foi entregue' in wire and 'delivery_deadline_exceeded' in wire
    assert 'corpo secreto' not in wire


def test_warning_waits_while_the_sender_pane_is_busy(world):
    db, rt, clock, w = world
    rt.status('a', 'working')
    n = notice(db, 'n')
    clock[0] = 1000 + 24 * HOUR
    for _ in range(3):
        w.tick()
    (row,) = failure_notices(db, n['id'])
    assert row['transport'] == 'deferred'
    clock[0] += 10 * HOUR  # a notice never expires by delivery deadline
    w.tick()
    assert db.get(row['id'])['transport'] == 'deferred'
    rt.status('a', 'idle')
    w.tick()
    assert db.get(row['id'])['transport'] == 'submitted'


# 3. A request that expects an answer still warns once, now naming the recipient.
def test_failed_request_warns_once_naming_the_recipient(world):
    db, rt, clock, w = world
    req = db.send(endpoint('a'), endpoint('b'), 'Pedido de build', 'x', 'r', now=1000)
    clock[0] = 1000 + REQUEST_SEND_TIMEOUT
    w.tick()
    assert state(db, req['id']) == ('failed', 'delivery_deadline_exceeded')
    rt.status('a', 'idle')
    w.tick()
    w.tick()
    assert len(failure_notices(db, req['id'])) == 1
    wire = [c for c in rt.calls if c[0] == endpoint('a')['pane_id']][0][1]
    assert 'Pedido de build' in wire and 'para b' in wire and '15 min' in wire


# 4. A failure warning that itself fails never produces another warning.
def test_failed_warning_does_not_generate_a_warning(world):
    db, rt, clock, w = world
    n = notice(db, 'n')
    clock[0] = 1000 + 24 * HOUR
    w.tick()
    (row,) = failure_notices(db, n['id'])
    rt.agents[endpoint('a')['pane_id']]['agent_session'] = dict(kind='id', source='herdr:codex', value='new-incarnation')
    w.tick()
    assert db.get(row['id'])['transport'] == 'failed'
    for _ in range(3):
        w.tick()
    assert db.db.execute("SELECT count(*) FROM messages WHERE kind='notice'").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


def test_superseded_or_replied_requests_are_not_reported_as_failures(world):
    db, rt, clock, w = world
    rt.status('b', 'idle')
    req = db.send(endpoint('a'), endpoint('b'), 'Pedido', 'x', 'r', now=1000)
    w.tick()
    db.reply(req['id'], endpoint('b'), 'pronto', 'k', now=1001)
    w.tick()
    assert failure_notices(db, req['id']) == []


# 5. One refusal per destination per tick, and health --deferred.
def test_one_refusal_per_destination_per_tick(world):
    db, rt, clock, w = world
    for i in range(5):
        notice(db, f'n{i}')
    notice(db, 'other', to='c')
    w.tick()
    assert rt.prepared.count(endpoint('b')['pane_id']) == 1
    assert rt.prepared.count(endpoint('c')['pane_id']) == 1
    assert db.db.execute("SELECT count(*) FROM messages WHERE transport='deferred'").fetchone()[0] == 5
    rt.prepared.clear()
    w.tick()
    assert rt.prepared.count(endpoint('b')['pane_id']) == 1  # the cache never survives the tick
    rt.status('b', 'idle')
    w.tick()
    assert len(rt.calls) == 6 and all(c[0] == endpoint('b')['pane_id'] for c in rt.calls[1:])
    assert db.db.execute("SELECT count(*) FROM messages WHERE transport='deferred'").fetchone()[0] == 0


def test_direct_deliver_never_reuses_a_stale_refusal(world):
    db, rt, clock, w = world
    n = notice(db, 'n')
    w.deliver(n['id'])
    assert state(db, n['id']) == ('deferred', 'target_busy')
    rt.status('b', 'idle')
    w.deliver(n['id'])
    assert state(db, n['id']) == ('submitted', None)


def test_endpoint_change_fails_every_message_to_that_destination(world):
    db, rt, clock, w = world
    ids = [notice(db, f'n{i}')['id'] for i in range(3)]
    rt.agents[endpoint('b')['pane_id']]['agent_session'] = dict(kind='id', source='herdr:codex', value='other')
    w.tick()
    assert [state(db, i) for i in ids] == [('failed', 'endpoint_changed')] * 3
    assert rt.prepared.count(endpoint('b')['pane_id']) == 1


def args(**kw):
    return SimpleNamespace(command='health', deferred=True, **kw)


def test_health_deferred_reports_counts_and_age_without_content(world, monkeypatch):
    db, rt, clock, w = world
    monkeypatch.setattr(cli, 'healthy', lambda _: True)
    monkeypatch.setattr(cli, 'DEFERRED_PILOT_DESTINATIONS', frozenset({'b', 'c'}))
    rt.status('c', 'working')
    notice(db, 'n1', 'Assunto reservado')
    notice(db, 'n2', 'Assunto reservado')
    notice(db, 'n3', to='c')
    w.tick()
    watcher = SimpleNamespace(caller=lambda: {**endpoint('vigia'), 'name': 'cerebro-vigia'})
    out = cli.execute(args(), db, watcher)
    assert out['code'] == 'worker_healthy'
    by_name = {r['destination']: r for r in out['deferred']}
    assert by_name['b']['deferred'] == 2 and by_name['c']['deferred'] == 1
    assert all(isinstance(r['oldest_age_seconds'], int) and r['oldest_age_seconds'] >= 0 for r in out['deferred'])
    dumped = json.dumps(out)
    assert 'Assunto reservado' not in dumped and 'corpo secreto' not in dumped
    assert set(out['deferred'][0]) == {'destination', 'deferred', 'oldest_age_seconds'}


def test_health_deferred_other_callers_see_only_their_own_queue(world, monkeypatch):
    db, rt, clock, w = world
    monkeypatch.setattr(cli, 'healthy', lambda _: True)
    monkeypatch.setattr(cli, 'DEFERRED_PILOT_DESTINATIONS', frozenset({'b', 'c'}))
    rt.status('c', 'working')
    notice(db, 'n1')
    notice(db, 'n2', to='c')
    w.tick()
    as_b = SimpleNamespace(caller=lambda: endpoint('b'))
    assert [r['destination'] for r in cli.execute(args(), db, as_b)['deferred']] == ['b']
    as_a = SimpleNamespace(caller=lambda: endpoint('a'))
    assert cli.execute(args(), db, as_a)['deferred'] == []


def test_health_deferred_observer_never_sees_destinations_outside_the_pilot(world, monkeypatch):
    db, rt, clock, w = world
    monkeypatch.setattr(cli, 'healthy', lambda _: True)
    for n in ('coordenador', 'guardian-exec', 'fora-do-piloto-exec'):
        rt.agents[endpoint(n)['pane_id']] = {**endpoint(n), 'agent_status': 'working'}
        db.send(endpoint('a'), endpoint(n), 'S', 'corpo', f'k-{n}', now=1000, requires_reply=False)
    w.tick()
    watcher = SimpleNamespace(caller=lambda: {**endpoint('vigia'), 'name': 'cerebro-vigia'})
    out = cli.execute(args(), db, watcher)
    assert [r['destination'] for r in out['deferred']] == ['coordenador']
    own = SimpleNamespace(caller=lambda: endpoint('guardian-exec'))
    assert [r['destination'] for r in cli.execute(args(), db, own)['deferred']] == ['guardian-exec']


def test_plain_health_needs_no_proof_and_has_no_deferred_field(world, monkeypatch):
    db, *_ = world
    monkeypatch.setattr(cli, 'healthy', lambda _: True)
    notice(db, 'n1')

    def no_proof():
        raise MessageError('identity_unproven')

    out = cli.execute(SimpleNamespace(command='health', deferred=False), db, SimpleNamespace(caller=no_proof))
    assert out == dict(code='worker_healthy')
    with pytest.raises(MessageError, match='identity_unproven'):
        cli.execute(args(), db, SimpleNamespace(caller=no_proof))


def test_cli_send_uses_the_notice_default_and_keeps_an_explicit_timeout(world, monkeypatch, tmp_path):
    db, *_ = world
    monkeypatch.setattr(cli, 'healthy', lambda _: True)
    body = tmp_path / 'body'
    body.write_text('Aviso')
    rt = SimpleNamespace(caller=lambda: endpoint('a'), get=lambda _: endpoint('b'))
    base = dict(command='send', target='b', subject='S', file=str(body), reply_timeout=900)
    default = db.get(cli.execute(SimpleNamespace(key='k1', notice=True, send_timeout=None, **base), db, rt)['id'])
    assert abs(default['send_deadline'] - default['created'] - 24 * HOUR) < 1
    request = db.get(cli.execute(SimpleNamespace(key='k2', notice=False, send_timeout=None, **base), db, rt)['id'])
    assert abs(request['send_deadline'] - request['created'] - 900) < 1
    explicit = db.get(cli.execute(SimpleNamespace(key='k3', notice=True, send_timeout=120, **base), db, rt)['id'])
    assert abs(explicit['send_deadline'] - explicit['created'] - 120) < 1


# --- achados da rodada 213 ---------------------------------------------------

def test_history_before_the_first_worker_is_not_announced(tmp_path):
    clock = [100]
    with Ledger(tmp_path / 'state', 'test') as db:
        old = notice(db, 'old', now=100)           # failed before any Worker existed
        db.fail(old['id'], 'delivery_deadline_exceeded', now=1000)
        pending = notice(db, 'pending', now=100, send_timeout=5000)  # created before, fails after
        clock[0] = 2000
        rt = Runtime()
        w = Worker(db, rt, clock=lambda: clock[0])
        rt.status('a', 'idle')
        clock[0] = 6000                              # pending expires now
        w.tick()
        w.tick()
        assert failure_notices(db, old['id']) == []
        assert len(failure_notices(db, pending['id'])) == 1
        fresh = notice(db, 'fresh', now=6000, send_timeout=10)
        clock[0] = 6010
        w.tick()
        w.tick()
        assert len(failure_notices(db, fresh['id'])) == 1
        # a restarted worker keeps the marker: old history stays quiet
        w2 = Worker(db, rt, clock=lambda: clock[0] + 99999)
        w2.tick()
        assert failure_notices(db, old['id']) == []


def test_requests_with_reply_keep_reporting_old_failures(tmp_path):
    clock = [100]
    with Ledger(tmp_path / 'state', 'test') as db:
        req = db.send(endpoint('a'), endpoint('b'), 'Pedido', 'x', 'r', now=100)
        db.fail(req['id'], 'delivery_deadline_exceeded', now=1000)
        clock[0] = 5000
        rt = Runtime()
        rt.status('a', 'idle')
        w = Worker(db, rt, clock=lambda: clock[0])
        w.tick()
        assert len(failure_notices(db, req['id'])) == 1


def test_cached_refusal_survives_a_reply_recorded_meanwhile(world, monkeypatch):
    db, rt, clock, w = world
    notice(db, 'first', now=1000)                                   # creates the cached refusal for b
    req = db.send(endpoint('a'), endpoint('b'), 'Pedido', 'x', 'r', now=1001)
    other = notice(db, 'other', to='c', now=1002)
    original = w._refuse

    def racing(mid, message, reason, now, **kw):
        if message['id'] == req['id']:
            db.reply(req['id'], endpoint('b'), 'pronto', 'k', now=now)
        return original(mid, message, reason, now, **kw)

    monkeypatch.setattr(w, '_refuse', racing)
    w.tick()                                                        # must not raise
    assert db.get(req['id'])['response'] == 'reply_recorded'
    assert db.get(req['id'])['transport'] == 'failed'
    assert state(db, other['id']) == ('submitted', None)
    assert not any(wire for pane, wire in rt.calls if 'Pedido' in wire)


def test_cached_endpoint_change_survives_a_reply_recorded_meanwhile(world, monkeypatch):
    db, rt, clock, w = world
    rt.agents[endpoint('b')['pane_id']]['agent_session'] = dict(kind='id', source='herdr:codex', value='other')
    notice(db, 'first', now=1000)
    req = db.send(endpoint('a'), endpoint('b'), 'Pedido', 'x', 'r', now=1001)
    original = w._refuse

    def racing(mid, message, reason, now, **kw):
        if message['id'] == req['id']:
            db.reply(req['id'], endpoint('b'), 'pronto', 'k', now=now)
        return original(mid, message, reason, now, **kw)

    monkeypatch.setattr(w, '_refuse', racing)
    w.tick()
    assert db.get(req['id'])['response'] == 'reply_recorded'


def test_long_queue_is_delivered_oldest_first_across_batches(world):
    db, rt, clock, w = world
    rt.status('c', 'working')
    for i in range(40):
        notice(db, f'c{i}', f'C{i:02d}', to='c', now=1000 + i)
    for i in range(20):
        notice(db, f'b{i}', f'B{i:02d}', to='b', now=1100 + i)
    w.tick()
    clock[0] += 1
    w.tick()
    rt.status('b', 'idle')
    for _ in range(3):
        clock[0] += 1
        w.tick()
    to_b = [wire for pane, wire in rt.calls if pane == endpoint('b')['pane_id']]
    assert len(to_b) == 20
    assert [f'B{i:02d}' in wire for i, wire in enumerate(to_b)] == [True] * 20


def test_requests_deep_in_a_long_queue_still_expire_on_time(world):
    db, rt, clock, w = world
    rt.status('c', 'working')
    ids = [db.send(endpoint('a'), endpoint('c'), 'P', 'x', f'r{i}', now=1000 + i)['id'] for i in range(60)]
    clock[0] = 1000 + REQUEST_SEND_TIMEOUT + 100
    w.tick()
    assert [state(db, i)[0] for i in ids] == ['failed'] * 60


def test_cached_refusal_is_not_rewritten_but_the_observed_head_is(world):
    db, rt, clock, w = world
    head = notice(db, 'head', now=1000)
    tail = notice(db, 'tail', now=1001)
    w.tick()
    head_updated, tail_updated = db.get(head['id'])['updated'], db.get(tail['id'])['updated']
    events = db.db.execute('SELECT count(*) FROM events').fetchone()[0]
    clock[0] += 50
    w.tick()
    assert db.get(head['id'])['updated'] == clock[0] != head_updated      # observed: rotates
    assert db.get(tail['id'])['updated'] == tail_updated                  # copied from the cache: untouched
    assert db.db.execute('SELECT count(*) FROM events').fetchone()[0] == events


def test_more_waiting_destinations_than_a_batch_never_starve_a_free_one(world):
    db, rt, clock, w = world
    for i in range(55):
        name = f'busy{i:02d}'
        rt.agents[endpoint(name)['pane_id']] = {**endpoint(name), 'agent_status': 'working'}
        notice(db, f'k{i}', to=name, now=1000 + i)
    w.tick()
    clock[0] += 1
    w.tick()
    rt.status('c', 'idle')
    free = notice(db, 'free', 'Livre', to='c', now=clock[0])
    reply_req = db.send(endpoint('a'), endpoint('c'), 'Pedido', 'x', 'req-c', now=clock[0])
    for _ in range(3):
        clock[0] += 1
        w.tick()
    assert state(db, free['id']) == ('submitted', None)
    assert state(db, reply_req['id']) == ('submitted', None)


def test_clock_going_backwards_does_not_silence_a_later_failure(world):
    db, rt, clock, w = world
    rt.status('a', 'idle')
    w.tick()                                           # activation at clock 1000
    late = notice(db, 'late', now=900)
    db.fail(late['id'], 'endpoint_changed', now=999)   # recorded after the cutoff, stamped earlier
    w.tick()
    assert len(failure_notices(db, late['id'])) == 1


def test_a_refused_worker_does_not_set_the_cutoff(world):
    from _herdr_message_runtime import file_lock
    db, rt, clock, w = world
    with file_lock(db.directory / 'worker.lock'):
        with pytest.raises(MessageError, match='reservation_busy'):
            w.run(once=True)
    assert db.meta('failure_notice_after_seq') is None


def test_late_notice_says_how_old_it_is_and_a_fresh_one_does_not(world):
    db, rt, clock, w = world
    notice(db, 'late', 'Atrasado', now=1000)
    w.tick()
    rt.status('b', 'idle')
    clock[0] = 1000 + 5 * HOUR + 12 * 60
    w.tick()
    wire = rt.calls[0][1]
    assert '[entrega atrasada: enviada há 5 h 12 min]' in wire
    notice(db, 'fresh', 'Novo', now=clock[0])
    w.tick()
    assert 'entrega atrasada' not in rt.calls[1][1]


def test_failure_text_tells_the_real_cause_and_asks_for_a_new_key(world):
    db, rt, clock, w = world
    rt.status('a', 'idle')
    stopped = notice(db, 'stopped')                  # no worker tick before the deadline
    clock[0] = 1000 + 24 * HOUR
    w.tick()
    w.tick()
    busy = notice(db, 'busy', now=clock[0], send_timeout=600)
    w.tick()
    clock[0] += 600
    w.tick()
    w.tick()
    wires = [wire for pane, wire in rt.calls if pane == endpoint('a')['pane_id']]
    stopped_wire = next(x for x in wires if stopped['id'] in x)
    busy_wire = next(x for x in wires if busy['id'] in x)
    assert 'sem nenhuma tentativa de entrega' in stopped_wire and 'prazo de 24 h' in stopped_wire
    assert 'último motivo de espera: target_busy' in busy_wire and 'prazo de 10 min' in busy_wire
    assert '--key nova' in stopped_wire and '--key nova' in busy_wire
    assert 'ocupado ou bloqueado' not in stopped_wire


def test_batch_gives_every_destination_its_head_first(world):
    db, rt, clock, w = world
    rt.status('c', 'working')
    heads = [notice(db, f'c{i}', to='c', now=1000 + i)['id'] for i in range(60)]
    last = notice(db, 'b-last', 'B', to='b', now=2000)
    batch = db.delivery_batch(50)
    assert batch[:2] == [heads[0], last['id']]
    assert batch[2:] == heads[1:49]


def test_a_request_blocked_by_an_open_request_does_not_hold_a_ready_reply(world):
    db, rt, clock, w = world
    rt.status('b', 'idle')
    first = db.send(endpoint('a'), endpoint('b'), 'Primeiro pedido', 'x', 'open', now=1000)
    w.tick()
    assert db.get(first['id'])['response'] == 'awaiting_reply'                  # open request to b
    blocked = db.send(endpoint('a'), endpoint('b'), 'Segundo pedido', 'x', 'blocked', now=1001)
    asked = db.send(endpoint('b'), endpoint('a'), 'Pergunta de b', 'x', 'ask', now=1002)
    rt.status('a', 'idle')
    w.tick()
    ready = db.reply(asked['id'], endpoint('a'), 'resposta', 'k', now=1003)    # a -> b, b is free
    plain = notice(db, 'plain', 'Aviso', now=1004)
    for i in range(50):
        name = f'busy{i:02d}'
        rt.agents[endpoint(name)['pane_id']] = {**endpoint(name), 'agent_status': 'working'}
        notice(db, f'k{i}', to=name, now=1005 + i)
    rt.calls.clear()
    for _ in range(3):
        clock[0] += 1
        w.tick()
    assert state(db, ready['id']) == ('submitted', None)
    assert state(db, plain['id']) == ('submitted', None)
    assert state(db, blocked['id'])[0] in ('queued', 'deferred')               # still barred, still visible
    assert not any('Segundo pedido' in wire for _, wire in rt.calls)
    db.reply(first['id'], endpoint('b'), 'feito', 'k2', now=clock[0])          # the open request is answered
    clock[0] += 1
    w.tick()
    assert state(db, blocked['id']) == ('submitted', None)


def test_blocked_request_still_expires_on_time(world):
    db, rt, clock, w = world
    rt.status('b', 'idle')
    db.send(endpoint('a'), endpoint('b'), 'Aberto', 'x', 'open', now=1000)
    w.tick()
    blocked = db.send(endpoint('a'), endpoint('b'), 'Barrado', 'x', 'blocked', now=1001)
    clock[0] = 1001 + REQUEST_SEND_TIMEOUT
    w.tick()
    assert state(db, blocked['id']) == ('failed', 'delivery_deadline_exceeded')


def test_cutoff_is_not_rewritten_on_every_tick(world):
    db, rt, clock, w = world
    w.tick()
    statements = []
    db.db.set_trace_callback(statements.append)
    db.mark_failure_cutoff()
    db.db.set_trace_callback(None)
    assert not any('BEGIN' in sql.upper() or 'INSERT' in sql.upper() for sql in statements)


def test_open_request_lookup_uses_the_partial_index(world):
    db, rt, clock, w = world
    plan = ' '.join(str(r[3]) for r in db.db.execute("""EXPLAIN QUERY PLAN SELECT 1 FROM messages m WHERE EXISTS (
        SELECT 1 FROM messages o WHERE o.destination=m.destination AND o.id!=m.id AND o.kind='request'
        AND o.transport='submitted' AND o.response='awaiting_reply')"""))
    assert 'open_requests' in plan
