import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))


def load(filename):
    loader = SourceFileLoader(filename.replace('-', '_') + '_selective', str(BIN / filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec); loader.exec_module(module)
    return module


@pytest.mark.parametrize('role', ['rev-1', 'rev-2'])
@pytest.mark.parametrize('legacy_name', [False, True])
def test_legacy_bootstrap_starts_only_selected_reviewer(tmp_path, monkeypatch, role, legacy_name):
    boot = load('herdr-bootstrap'); root = str(tmp_path)
    monkeypatch.setattr(boot.startup,'created_pane_receipt',lambda *a,**kw:None)
    monkeypatch.setattr(boot, 'SPACES', [('sample', root, ('sample-exec', 'codex', []),
                                      ('sample-scout', 'codex', []), 'sample', {'lean': True})])
    monkeypatch.setattr(boot.core, 'project_root', lambda p: root)
    monkeypatch.setattr(boot.migration, 'space_gate_message', lambda *a: None)
    monkeypatch.setattr(boot, 'build_rev_agents', lambda *a: [
        ('sample-rev' if legacy_name else 'sample-rev-1', 'codex', []), ('sample-rev-2', 'claude', [])])
    monkeypatch.setattr(boot, 'find_unnamed_reviewer_base', lambda *a, **kw: None)
    monkeypatch.setattr(boot, 'ensure_notify_watchers', lambda *a: None)
    def api(*args):
        if args == ('workspace', 'list'): return {'workspaces': [{'label': 'sample', 'workspace_id': 'w1'}]}
        if args == ('pane', 'list'): return {'panes': [{'pane_id': 'w1:p1', 'tab_id': 'w1:t1', 'workspace_id': 'w1'}]}
        if args == ('agent', 'list'): return {'agents': [{'name': 'sample-exec', 'pane_id': 'w1:p1',
                                                       'tab_id': 'w1:t1', 'workspace_id': 'w1'}]}
        raise AssertionError(args)
    monkeypatch.setattr(boot, 'api', api)
    created = []
    def run(*args):
        assert args[:2] == ('pane', 'split')
        return {'pane': {'pane_id': 'w1:p2'}}
    monkeypatch.setattr(boot, 'run', run)
    monkeypatch.setattr(boot, 'start_agent_with_role', lambda pane, name, *a, **kw: created.append(name))
    monkeypatch.setattr(sys, 'argv', ['herdr-bootstrap', '--slug', 'sample', '--role', role])
    assert boot.main() == 0
    assert created == ['sample-rev' if legacy_name and role == 'rev-1' else 'sample-' + role]


def test_selected_only_recovery_passes_role_not_pair_to_bootstrap(tmp_path, monkeypatch):
    core = load('_herdr_dispatch.py'); commands = []
    def get(name):
        if not commands: raise RuntimeError('agent_not_found')
        return {'agent_status': 'idle', 'pane_id': 'w:p1', 'agent': 'claude'}
    monkeypatch.setattr(core, 'get_agent_info', get)
    monkeypatch.setattr(core, '_wait_restored_agent', lambda n: get(n))
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(core.subprocess, 'run', run)
    core.ensure_required_agents('sample', str(tmp_path), ['sample-rev-2'], selected_only=True)
    assert commands[0][-2:] == ['--role', 'rev-2']
    assert '--with-reviewers' not in commands[0]
