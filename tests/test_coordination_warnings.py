import sys
from pathlib import Path
from unittest.mock import Mock
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import _herdr_coordination_warnings as warnings


def test_blocking_command_requires_observed_command_and_duration():
    assert warnings.blocking_command(['sleep', '90'])
    assert warnings.blocking_command(['herdr', 'agent', 'prompt', 'x', 'hi', '--wait', '--timeout', '300000'])
    assert warnings.blocking_command(['sleep', '1']) is None
    assert warnings.blocking_command(['opencode']) is None


def test_warning_does_not_assert_v1_from_kind_alone(monkeypatch, capsys):
    monkeypatch.setattr(warnings, 'process', lambda pid: dict(pid=pid, argv=['sleep', '90'], executable='/bin/sleep'))
    query = lambda *args: {'process_info': {'foreground_processes': [{'pid': 1, 'argv': ['sleep', '90']}]}}
    warnings.emit('herdr-can-send', target={'agent': 'opencode', 'pane_id': 'w:p'}, query=query)
    assert capsys.readouterr().err == ''


def test_verified_v1_and_blocking_process_produce_advisory(monkeypatch, capsys):
    monkeypatch.setattr(warnings, 'process', lambda pid: dict(pid=pid, argv=['opencode'] if pid == 1 else ['sleep', '90'], executable='/test/opencode' if pid == 1 else '/bin/sleep'))
    monkeypatch.setattr(warnings, 'opencode1', lambda p: p['pid'] == 1)
    query = lambda *args: {'process_info': {'foreground_processes': [{'pid': 1, 'argv': ['opencode']}, {'pid': 2, 'argv': ['sleep', '90']}]}}
    warnings.emit('herdr-can-send', target={'agent': 'opencode', 'pane_id': 'w:p'}, query=query)
    assert 'não libera envio nem interrompe' in capsys.readouterr().err


def test_dispatch_warning_preserves_artifact_contract(monkeypatch, capsys):
    monkeypatch.setattr(warnings, 'caller_opencode1', lambda: {'pid': 1})
    warnings.emit('herdr-ask')
    assert 'só conclui com artefato e exit code' in capsys.readouterr().err


def test_version_two_never_treated_as_one(monkeypatch):
    monkeypatch.setattr(warnings.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 0, '2.0.0\n', '')))
    assert not warnings.opencode1(dict(argv=['opencode'], executable='/test/opencode'))


def test_interpreter_version_is_not_opencode_version(monkeypatch):
    run = Mock()
    monkeypatch.setattr(warnings.subprocess, 'run', run)
    assert not warnings.opencode1(dict(argv=['opencode'], executable='/usr/bin/bun'))
    run.assert_not_called()


import importlib.util
from importlib.machinery import SourceFileLoader
import pytest


@pytest.mark.parametrize('shape', [None, [], 'invalid'])
def test_malformed_optional_process_info_never_escapes(shape):
    warnings.emit('herdr-can-send', target={'agent': 'opencode', 'pane_id': 'w:p'},
                  query=lambda *args: {'process_info': shape})


@pytest.mark.parametrize('error', [IndexError('short stat'), AttributeError('missing attribute')])
def test_optional_observer_failure_never_escapes(monkeypatch, error):
    def broken():
        raise error
    monkeypatch.setattr(warnings, 'caller_opencode1', broken)
    warnings.emit('herdr-ask')


def load_entry(name):
    path = Path(__file__).resolve().parents[1]/'bin'/name
    loader = SourceFileLoader('advisory_entry_' + name.replace('-', '_'), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


@pytest.mark.parametrize('code', range(5))
def test_guard_entrypoint_keeps_original_code_and_json_when_advisory_crashes(monkeypatch, capsys, code):
    import json
    m = load_entry('herdr-can-send')
    monkeypatch.setattr(sys, 'argv', ['herdr-can-send', 'target', '--json'])
    monkeypatch.setattr(m, 'resolver', lambda target: {'pane_id': 'w:p'})
    monkeypatch.setattr(m, 'decidir', lambda *args: (code, 'label', 'detail', 'type', ''))
    monkeypatch.setattr(m.core, 'effective_status', lambda info: 'idle')
    def broken(*args, **kwargs):
        raise AttributeError('optional observer failed')
    monkeypatch.setattr(warnings, 'emit', broken)
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == code
    assert json.loads(capsys.readouterr().out)['codigo'] == code


@pytest.mark.parametrize('entry', ['herdr-ask', 'herdr-review-dispatch'])
def test_dispatcher_reaches_original_gate_despite_optional_failure(monkeypatch, entry):
    m = load_entry(entry)
    class ReachedOriginalGate(Exception):
        pass
    def gate():
        raise ReachedOriginalGate()
    def broken(*args, **kwargs):
        raise IndexError('optional observer failed')
    monkeypatch.setattr(warnings, 'emit', broken)
    monkeypatch.setattr(m.core, 'require_herdr_pane', gate)
    args = ['x', '--question-file', '/not-read'] if entry == 'herdr-ask' else ['x', '--description', 'test']
    monkeypatch.setattr(sys, 'argv', [entry, *args])
    with pytest.raises(ReachedOriginalGate):
        m.main()
