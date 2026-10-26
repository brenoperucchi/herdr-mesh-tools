"""Durable request/reply boundaries, independent of panes and lifecycle."""
import concurrent.futures
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from _herdr_message_ledger import Ledger, MessageError


def endpoint(name):
    return dict(name=name, agent='codex', pane_id=f'w1:p{name}', workspace_id='w1',
                terminal_id=f'term-{name}', cwd='/tmp/project',
                agent_session=dict(kind='id', source='herdr:codex', value=f'session-{name}'))


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / 'private', 'test-server') as db:
        yield db


def request(db, key='req', **kw):
    return db.send(endpoint('a'), endpoint('b'), 'Build', 'Do the build', key,
                   now=100, **kw)


def test_request_and_reply_are_durable_before_delivery(ledger):
    req = request(ledger)
    assert req['transport'] == 'queued'
    reply = ledger.reply(req['id'], endpoint('b'), 'Ready', 'reply', now=110)
    assert reply['transport'] == 'queued'
    state = ledger.status(req['id'])
    assert state['response'] == 'reply_recorded'
    assert state['reply']['id'] == reply['id']
    assert ledger.body(reply['id']) == 'Ready'


def test_idempotency_conflicting_payload_and_single_final_reply(ledger):
    req = request(ledger)
    assert request(ledger)['id'] == req['id']
    with pytest.raises(MessageError):
        ledger.send(endpoint('a'), endpoint('b'), 'Build', 'Changed', 'req', now=100)
    first = ledger.reply(req['id'], endpoint('b'), 'Ready', 'r', now=110)
    assert ledger.reply(req['id'], endpoint('b'), 'Ready', 'r', now=111)['id'] == first['id']
    with pytest.raises(MessageError):
        ledger.reply(req['id'], endpoint('b'), 'Different', 'r2', now=112)


def test_reply_requires_original_incarnation(ledger):
    req = request(ledger)
    other = endpoint('b')
    other['agent_session']['value'] = 'replacement'
    with pytest.raises(MessageError):
        ledger.reply(req['id'], other, 'Ready', 'r', now=110)


def test_ryzen9_reply_persists_while_origin_busy(ledger):
    req = request(ledger)
    attempt = ledger.begin(req['id'], now=101)
    ledger.finish(req['id'], attempt, 'submitted', now=102)
    reply = ledger.reply(req['id'], endpoint('b'), 'Build ready', 'r', now=110)
    ledger.defer(reply['id'], 'origin_busy', now=111)
    assert ledger.status(req['id'])['state'] == 'return_pending_origin_busy'
    assert ledger.body(reply['id']) == 'Build ready'


def test_crash_after_intent_is_unknown_never_queued_again(ledger):
    req = request(ledger)
    ledger.begin(req['id'], now=101)
    ledger.recover(now=102)
    assert ledger.status(req['id'])['transport'] == 'unknown'
    assert not ledger.pending()
    with pytest.raises(MessageError):
        ledger.begin(req['id'], now=103)


def test_confirmed_submission_cannot_be_sent_again(ledger):
    req = request(ledger)
    token = ledger.begin(req['id'], now=101)
    ledger.finish(req['id'], token, 'submitted', now=102)
    assert not ledger.pending()
    with pytest.raises(MessageError):
        ledger.begin(req['id'], now=103)


def test_notice_never_creates_reply_obligation(ledger):
    req = request(ledger, requires_reply=False)
    assert ledger.status(req['id'])['response'] == 'not_required'
    with pytest.raises(MessageError):
        ledger.reply(req['id'], endpoint('b'), 'No loop', 'r', now=110)


def test_scope_and_permissions(tmp_path):
    path = tmp_path / 'private'
    with Ledger(path, 'server-a') as db:
        request(db)
        assert path.stat().st_mode & 0o777 == 0o700
        for f in path.glob('ledger.sqlite3*'):
            assert f.stat().st_mode & 0o077 == 0
    with pytest.raises(MessageError):
        Ledger(path, 'server-b')


def test_concurrent_same_request_is_one_message(tmp_path):
    path = tmp_path / 'private'
    with Ledger(path, 'test-server'):
        pass
    def insert(_):
        with Ledger(path, 'test-server') as db:
            return request(db)['id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(insert, range(12)))
    assert len(set(ids)) == 1


@pytest.mark.parametrize('subject', ['é' * 150, '🦀' * 75, 'x' * 295 + 'éé'])
def test_unicode_subject_never_prevents_reply(ledger, subject):
    req = ledger.send(endpoint('a'), endpoint('b'), subject, 'Work', 'unicode', now=100)
    reply = ledger.reply(req['id'], endpoint('b'), 'Done', 'unicode-reply', now=101)
    assert len(reply['subject'].encode()) <= 300


def test_reply_replay_after_submission_is_noop(ledger):
    req = request(ledger)
    reply = ledger.reply(req['id'], endpoint('b'), 'Done', 'r', now=101)
    token = ledger.begin(reply['id'], now=102)
    ledger.finish(reply['id'], token, 'submitted', now=103)
    before = ledger.get(req['id'])
    ledger.reply(req['id'], endpoint('b'), 'Done', 'r', now=104)
    assert ledger.get(req['id']) == before
    assert ledger.db.execute("SELECT count(*) FROM events WHERE message_id=? AND event='reply_recorded'", (req['id'],)).fetchone()[0] == 1


def test_defer_event_and_terminal_mutation_refused(ledger):
    req = request(ledger)
    ledger.defer(req['id'], 'origin_busy', now=101)
    assert ledger.db.execute("SELECT detail FROM events WHERE event='deferred'").fetchone()[0] == 'origin_busy'
    token = ledger.begin(req['id'], now=102)
    ledger.finish(req['id'], token, 'submitted', now=103)
    count = ledger.db.execute('SELECT count(*) FROM events').fetchone()[0]
    for method in (ledger.defer, ledger.fail):
        with pytest.raises(MessageError):
            method(req['id'], 'bad', now=104)
    assert ledger.db.execute('SELECT count(*) FROM events').fetchone()[0] == count


def test_answered_queued_request_is_removed_from_delivery_queue(ledger):
    req = request(ledger)
    ledger.reply(req['id'], endpoint('b'), 'Already done', 'r', now=101)
    assert req['id'] not in ledger.pending()
    assert ledger.get(req['id'])['reason'] == 'superseded_by_reply'


def test_kind_namespaces_and_transactional_system_messages(ledger):
    req = request(ledger)
    ledger.send(endpoint('b'), endpoint('a'), 'Other', 'Work', 'same', now=100)
    ledger.reply(req['id'], endpoint('b'), 'Done', 'same', now=101)
    with pytest.raises(MessageError):
        ledger.system_message(req['id'], 'notice', 'status', 102)
    with ledger.transaction():
        a = ledger.system_message(req['id'], 'notice', 'status', 102)
        b = ledger.system_message(req['id'], 'notice', 'status', 103)
    assert a['id'] == b['id']


def test_retry_after_proven_refusal_and_stale_receipt(ledger):
    req = request(ledger)
    first = ledger.begin(req['id'], now=101)
    ledger.finish(req['id'], first, 'deferred', now=102)
    second = ledger.begin(req['id'], now=103)
    with pytest.raises(MessageError):
        ledger.finish(req['id'], first, 'submitted', now=104)
    ledger.finish(req['id'], second, 'unknown', now=105)
    with pytest.raises(MessageError):
        ledger.fail(req['id'], 'no', now=106)


def test_strict_endpoint_policy_refuses_renamed_or_relocated_caller(ledger):
    req = request(ledger)
    for key in ('name', 'cwd'):
        changed = endpoint('b')
        changed[key] = 'changed'
        with pytest.raises(MessageError):
            ledger.reply(req['id'], changed, 'Done', 'r', now=101)


def test_rejects_open_directory_symlink_and_hardlink(tmp_path):
    import os
    unsafe = tmp_path/'unsafe'
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    with pytest.raises(MessageError):
        Ledger(unsafe, 'test')
    real = tmp_path/'real'
    real.mkdir(mode=0o700)
    link = tmp_path/'linked'
    link.symlink_to(real)
    with pytest.raises(MessageError):
        Ledger(link, 'test')
    (real/'ledger.sqlite3').touch(mode=0o600)
    os.link(real/'ledger.sqlite3', tmp_path/'hardlink')
    with pytest.raises(MessageError):
        Ledger(real, 'test')


def test_recording_reply_cancels_pending_reminder(ledger):
    req = request(ledger)
    token = ledger.begin(req['id'], now=101)
    ledger.finish(req['id'], token, 'submitted', now=102)
    with ledger.transaction():
        reminder = ledger.system_message(req['id'], 'reminder', 'Please reply', 103)
    ledger.reply(req['id'], endpoint('b'), 'Ready', 'r', now=104)
    assert reminder['id'] not in ledger.pending()
    assert ledger.get(reminder['id'])['reason'] == 'superseded_by_reply'
    with pytest.raises(MessageError):
        ledger.begin(reminder['id'], now=105)


@pytest.mark.parametrize('kind', ['request', 'reminder'])
def test_late_proven_refusal_does_not_requeue_answered_work(ledger, kind):
    req = request(ledger)
    mid = req['id']
    if kind == 'reminder':
        tok = ledger.begin(mid, now=101)
        ledger.finish(mid, tok, 'submitted', now=102)
        with ledger.transaction():
            mid = ledger.system_message(req['id'], 'reminder', 'Reply', 103)['id']
    token = ledger.begin(mid, now=104)
    ledger.reply(req['id'], endpoint('b'), 'Done', 'r', now=105)
    ledger.finish(mid, token, 'deferred', now=106, reason='agent_busy')
    assert mid not in ledger.pending()
    assert ledger.get(mid)['reason'] == 'superseded_by_reply'
    with pytest.raises(MessageError):
        ledger.begin(mid, now=107)
