import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import _herdr_message_runtime as module
from _herdr_message_ledger import MessageError
from test_message_ledger import endpoint


@pytest.fixture
def rt():
    return module.Runtime(env={'HERDR_SOCKET_PATH': '/tmp/test-herdr-socket'})


def receipt(info):
    return json.dumps(dict(result=dict(type='agent_prompted', agent=info)))


def test_submit_only_supported_surface_no_wait_no_interrupt(rt, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, receipt(endpoint('b')), ''))
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert rt.submit(endpoint('b'), 'hello') == ('submitted', None)
    argv = run.call_args.args[0]
    assert argv[1:] == ['agent', 'prompt', endpoint('b')['pane_id'], 'hello']
    assert not any(x in argv for x in ('--wait', '--allow-working', 'send-keys', 'ctrl+c', 'run'))


@pytest.mark.parametrize('value', ['', '{}', '{"result":{"type":"agent_info"}}'])
def test_no_valid_receipt_means_unknown(rt, monkeypatch, value):
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 0, value, '')))
    assert rt.submit(endpoint('b'), 'hello')[0] == 'unknown'


def test_timeout_after_possible_delivery_is_unknown(rt, monkeypatch):
    monkeypatch.setattr(module.subprocess, 'run', Mock(side_effect=subprocess.TimeoutExpired('herdr', 30)))
    assert rt.submit(endpoint('b'), 'hello')[0] == 'unknown'


def test_receipt_wrong_session_is_unknown(rt, monkeypatch):
    changed = endpoint('b')
    changed['agent_session']['value'] = 'new'
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 0, receipt(changed), '')))
    assert rt.submit(endpoint('b'), 'hello')[0] == 'unknown'


def test_prequeue_blocked_refusal_can_be_rechecked(rt, monkeypatch):
    error = json.dumps(dict(error=dict(code='agent_blocked')))
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 1, '', error)))
    assert rt.submit(endpoint('b'), 'hello') == ('deferred', 'agent_blocked')


def test_unknown_is_never_allowed_by_guard_even_if_code_zero(rt, monkeypatch):
    data = dict(codigo=0, pode_enviar=True, pane=endpoint('b')['pane_id'], status='unknown')
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(data), '')))
    assert rt.guard(endpoint('b')) == (False, 'guard_refused')


def test_changed_metadata_between_guard_and_send_refuses(rt, monkeypatch):
    before = {**endpoint('b'), 'agent_status': 'idle', 'state_change_seq': 1}
    after = {**before, 'state_change_seq': 2}
    monkeypatch.setattr(rt, 'get', Mock(side_effect=[before, after]))
    monkeypatch.setattr(rt, 'guard', Mock(return_value=(True, None)))
    monkeypatch.setattr(module.core, 'effective_status', lambda x: x['agent_status'])
    assert rt.prepare(endpoint('b'))[:2] == (False, 'preflight_changed')


def test_caller_uses_ancestry_not_focused_pane(rt, monkeypatch):
    chain = {10: 'start10', 9: 'start9'}
    monkeypatch.setattr(module, 'ancestors', lambda: chain)
    def read(*args):
        if args[:2] == ('agent', 'list'):
            return {'agents': [endpoint('wrong'), endpoint('a')]}
        pane = args[-1]
        match = pane == endpoint('a')['pane_id']
        return {'process_info': {'shell_pid': 9 if match else 99,
                                 'foreground_processes': [{'pid': 10 if match else 100}]}}
    monkeypatch.setattr(rt, 'read', read)
    monkeypatch.setattr(rt, 'get', lambda _: endpoint('a'))
    assert rt.caller() == endpoint('a')


def test_missing_or_ambiguous_caller_fails_closed(rt, monkeypatch):
    monkeypatch.setattr(module, 'ancestors', lambda: {10: 'start10', 9: 'start9'})
    monkeypatch.setattr(rt, 'read', lambda *a: {'agents': []})
    with pytest.raises(MessageError, match='identity_unproven'):
        rt.caller()


def test_reservation_nonblocking_and_shared_with_dispatch(rt, monkeypatch, tmp_path):
    lock = tmp_path / 'existing.lock'
    with module.file_lock(lock):
        with pytest.raises(MessageError, match='reservation_busy'):
            with module.file_lock(lock):
                pytest.fail('must not acquire a second reservation')


@pytest.mark.parametrize('value,rc', [
    ('null', 0), ('[]', 1), ('"bad"', 1), ('{"error":"unavailable"}', 1),
    ('{"result":{"type":"agent_prompted","agent":null}}', 0),
    ('{"result":{"type":"agent_prompted","agent":[]}}', 0),
])
def test_malformed_json_shapes_are_unknown_not_exceptions(rt, monkeypatch, value, rc):
    result = subprocess.CompletedProcess([], rc, value if rc == 0 else '', value if rc else '')
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=result))
    assert rt.submit(endpoint('b'), 'hello')[0] == 'unknown'


@pytest.mark.parametrize('value', ['null', '[]', '"bad"'])
def test_guard_malformed_shapes_refuse(rt, monkeypatch, value):
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 0, value, '')))
    assert rt.guard(endpoint('b')) == (False, 'guard_unreadable')


def test_reply_command_carries_complete_route_and_state(rt, tmp_path):
    import shlex
    args = shlex.split(rt.reply_command(tmp_path/'space in path', 'request-id'))
    assert args[args.index('--state-dir')+1] == str(tmp_path/'space in path')
    assert json.loads(args[args.index('--route')+1]) == rt.route
    assert args.index('--state-dir') < args.index('reply')


def test_runtime_reservation_matches_real_dispatcher_with_custom_tmpdir(rt, monkeypatch, tmp_path):
    monkeypatch.setattr(module.tempfile, 'gettempdir', lambda: str(tmp_path))
    info = endpoint('b')
    with module.core._dispatch_submission_locks({'b': info}):
        with pytest.raises(MessageError, match='reservation_busy'):
            with rt.reservation(info):
                pytest.fail('different lock path')
    with rt.reservation(info):
        other = endpoint('c')
        with module.core._dispatch_submission_locks({'c': other}):
            pass


@pytest.mark.parametrize('value', ['null', '[]', '{"result":[]}'])
def test_read_malformed_shapes_are_closed_errors(rt, monkeypatch, value):
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 0, value, '')))
    with pytest.raises(MessageError, match='herdr_read_failed'):
        rt.read('agent', 'list')


def test_reverse_dispatch_reservation_refuses_immediately(rt, monkeypatch, tmp_path):
    monkeypatch.setattr(module.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(module.core, 'DISPATCH_LOCK_WAIT_S', 0)
    with rt.reservation(endpoint('b')):
        with pytest.raises(module.core.DispatchLockBusyError):
            with module.core._dispatch_submission_locks({'b': endpoint('b')}):
                pytest.fail('different lock path')


@pytest.mark.parametrize('code', [[], {}, None, 3, 'agent_blocked'])
def test_error_code_shape_is_checked_before_refusal_membership(rt, monkeypatch, code):
    payload = json.dumps({'error': {'code': code}})
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 1, '', payload)))
    assert rt.submit(endpoint('b'), 'hello')[0] == ('deferred' if code == 'agent_blocked' else 'unknown')


def test_missing_endpoint_error_is_read_from_real_stderr_shape(rt, monkeypatch):
    payload = json.dumps({'error': {'code': 'agent_not_found'}})
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 1, '', payload)))
    with pytest.raises(MessageError, match='endpoint_missing'):
        rt.get(endpoint('a')['pane_id'])
    assert rt.prepare(endpoint('a'))[:2] == (False, 'endpoint_changed')
