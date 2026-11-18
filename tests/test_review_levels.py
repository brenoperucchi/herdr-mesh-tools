"""CLI regressions for D+B: coverage is never semantic approval."""
import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))


def load():
    loader = SourceFileLoader('review_levels_cli', str(BIN / 'herdr-review-dispatch'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec); loader.exec_module(module)
    return module


@pytest.fixture
def cli(tmp_path, monkeypatch):
    mod = load(); states = {'sample-rev-1': 'idle', 'sample-rev-2': 'idle'}
    sent = []; restored = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod.core, 'require_herdr_pane', lambda: None)
    monkeypatch.setattr(mod.core, 'project_root', lambda p: str(tmp_path))
    monkeypatch.setattr(mod.migration, 'space_gate_message', lambda *a: None)
    monkeypatch.setattr(mod.migration, 'resolve_reviewer_name', lambda *a, **k: 'sample-rev-1')
    def info(name):
        if name not in states:
            raise RuntimeError('agent_not_found')
        return {'name': name, 'agent': 'codex', 'agent_status': states[name],
                'pane_id': 'w:p' + name[-1], 'workspace_id': 'w', 'tab_id': 'w:t1',
                'agent_session': {'value': 'session-' + name}, 'cwd': str(tmp_path),
                'foreground_cwd': str(tmp_path), 'state_change_seq': 1, 'revision': 2}
    monkeypatch.setattr(mod.core, 'get_agent_info', info)
    monkeypatch.setattr(mod, 'get_agent_info', info)
    monkeypatch.setattr(mod, 'validate_agent_project', lambda n, c: info(n))
    monkeypatch.setattr(mod.core, 'effective_agent_status', lambda n: info(n)['agent_status'])
    monkeypatch.setattr(mod.core, 'effective_status', lambda i: i['agent_status'])
    monkeypatch.setattr(mod, 'runtime_profile_evidence', lambda *a: {})
    def restore(slug, cwd, names, **kw):
        restored.extend(names)
        for name in names:
            states.setdefault(name, 'idle')
        return {'restored': names}
    monkeypatch.setattr(mod.core, 'ensure_required_agents', restore)
    # wait_quiescent tem testes proprios (test_review_handshake); aqui seria espera real.
    monkeypatch.setattr(mod.core, 'wait_quiescent', lambda n, **k: info(n))
    def dispatch(prompts, timeout, required_artifacts, **kw):
        sent.extend(prompts)
        for name, path in required_artifacts.items():
            Path(path).write_text('APPROVE\n ação necessária: nenhuma')
        return ({n: 'done' for n in prompts}, {}, {})
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', dispatch)
    file = tmp_path / 'readme.txt'; file.write_text('small edit')
    def run(*args):
        monkeypatch.setattr(sys, 'argv', ['herdr-review-dispatch', 'sample', '--description',
            'unit', '--files', str(file), *args])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        metrics = sorted(tmp_path.glob('.herdr/review/*/metrics.json'))
        return exc.value.code, json.loads(metrics[-1].read_text()) if metrics else None
    return mod, states, sent, restored, run, tmp_path


def test_default_two_is_complete_coverage_not_approval(cli):
    _, _, sent, _, run, _ = cli
    code, metrics = run()
    assert code == 0 and len(sent) == 2
    assert metrics['nivel'] == 2 and metrics['review_status'] == 'completa'
    assert metrics['approval'] is False


def test_one_requires_justification_before_dispatch(cli):
    _, _, sent, _, run, _ = cli
    code, _ = run('--nivel', '1')
    assert code != 0 and not sent


def test_one_uses_first_ready_and_records_reason_in_request(cli):
    _, states, sent, restored, run, root = cli
    states['sample-rev-1'] = 'working'
    code, metrics = run('--nivel', '1', '--justificativa', 'texto apenas')
    assert code == 0 and sent == ['sample-rev-2']
    assert restored == ['sample-rev-2']
    assert metrics['justificativa'] == 'texto apenas'
    request = next(root.glob('.herdr/review/*/sample-rev-2/request.md')).read_text()
    assert 'texto apenas' in request and 'Nível de revisão: 1' in request
    assert 'outro revisor está avaliando' not in request


def test_one_explicit_choice_and_no_second_bootstrap(cli):
    _, states, sent, restored, run, _ = cli
    states.pop('sample-rev-1')
    code, _ = run('--nivel', '1', '--justificativa', 'texto', '--revisor', 'rev-2')
    assert code == 0 and sent == ['sample-rev-2'] and restored == ['sample-rev-2']
    assert 'sample-rev-1' not in states


def test_one_can_bootstrap_only_one_missing_role(cli):
    _, states, sent, restored, run, _ = cli
    states.clear()
    code, _ = run('--nivel', '1', '--justificativa', 'texto')
    assert code == 0 and sent == restored == ['sample-rev-1']
    assert 'sample-rev-2' not in states


def test_one_declared_trigger_refuses_before_restore(cli):
    _, _, sent, restored, run, _ = cli
    code, _ = run('--nivel', '1', '--justificativa', 'texto', '--gatilho', 'protocolo')
    assert code != 0 and not sent and not restored


def test_two_busy_degrades_and_never_approves(cli):
    _, states, sent, _, run, _ = cli
    states['sample-rev-2'] = 'working'
    code, metrics = run('--gatilho', 'contrato')
    assert code == 3 and metrics['review_status'] == 'parcial'
    assert sent == ['sample-rev-1']
    assert metrics['approval'] is False
    assert metrics['reviewers']['sample-rev-2']['status'] == 'unavailable'
    assert 'working' in metrics['reviewers']['sample-rev-2']['reason']
    assert metrics['requires_followup'] is True


@pytest.mark.parametrize('failed_status', ['blocked', 'timeout', 'artifact_missing', 'preflight_aborted'])
def test_two_dispatch_failure_degrades_even_if_file_exists(cli, monkeypatch, failed_status):
    mod, _, _, _, run, _ = cli
    def dispatch(prompts, timeout, required_artifacts, **kw):
        for path in required_artifacts.values(): Path(path).write_text('APPROVE')
        return ({'sample-rev-1': 'done', 'sample-rev-2': failed_status},
                {'sample-rev-2': {'reason': 'test refusal'}}, {})
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', dispatch)
    code, metrics = run()
    assert code == 3 and metrics['review_status'] == 'parcial'
    assert metrics['approval'] is False


def test_no_reviewer_success_is_failure_not_partial(cli):
    _, states, sent, _, run, root = cli
    states.update({n: 'blocked' for n in states})
    code, metrics = run()
    assert code == 1 and metrics is None and not sent
    assert not list(root.glob('.herdr/review/*'))


def test_restore_failure_of_one_does_not_abort_other(cli, monkeypatch):
    mod, states, sent, _, run, _ = cli
    states.pop('sample-rev-2')
    def restore(slug, cwd, names, **kw):
        if 'sample-rev-2' in names: raise mod.core.AgentRestoreError('not registered')
        return {}
    monkeypatch.setattr(mod.core, 'ensure_required_agents', restore)
    code, metrics = run()
    assert code == 3 and sent == ['sample-rev-1']
    assert 'not registered' in metrics['reviewers']['sample-rev-2']['reason']


def test_effective_idle_snapshot_is_used_without_second_unvalidated_baseline(cli, monkeypatch):
    mod, states, sent, _, run, _ = cli
    states['sample-rev-1'] = 'working'
    monkeypatch.setattr(mod.core, 'effective_status', lambda info: 'idle')
    code, _ = run()
    assert code == 0 and len(sent) == 2


def test_no_session_of_one_reviewer_degrades_without_sending(cli, monkeypatch):
    mod, _, sent, _, run, _ = cli
    original = mod.validate_agent_project
    def validate(name, root):
        info = original(name, root)
        if name.endswith('2'): info['agent_session'] = None
        return info
    monkeypatch.setattr(mod, 'validate_agent_project', validate)
    code, metrics = run()
    assert code == 3 and sent == ['sample-rev-1']
    assert 'sessão' in metrics['reviewers']['sample-rev-2']['reason']


def test_one_explicit_busy_does_not_interrupt_or_fallback(cli):
    _, states, sent, restored, run, root = cli
    states['sample-rev-2'] = 'working'
    code, metrics = run('--nivel', '1', '--justificativa', 'texto', '--revisor', 'rev-2')
    assert code == 1 and not sent and restored == ['sample-rev-2']
    assert states['sample-rev-2'] == 'working' and metrics is None
    assert not list(root.glob('.herdr/review/*'))


def test_diff_frozen_material_rechecked_before_send(cli, monkeypatch):
    import subprocess
    mod, _, sent, _, _, root = cli
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=T', '-c', 'user.email=t@localhost',
                    'commit', '--allow-empty', '-m', 'fixture'], check=True, capture_output=True)
    def freeze(cwd, base, out):
        (Path(out) / 'diff.patch').write_text(
            'diff --git a/.env b/.env\nnew file mode 100644\nindex 0000000..19a5ad2\n'
            '--- /dev/null\n+++ b/.env\n@@ -0,0 +1 @@\n+placeholder\n')
        return 'a' * 40
    monkeypatch.setattr(mod, 'freeze_diff', freeze)
    monkeypatch.setattr(sys, 'argv', ['herdr-review-dispatch', 'sample', '--nivel', '1',
                                    '--justificativa', 'texto', '--description', 'unit'])
    with pytest.raises(SystemExit) as exc: mod.main()
    assert exc.value.code == 2 and not sent


def complement_round(mod, root, monkeypatch, *args):
    folder = next(root.glob('.herdr/review/*/metrics.json')).parent
    monkeypatch.setattr(sys, 'argv', ['herdr-review-dispatch', 'sample', '--complementar', str(folder), *args])
    with pytest.raises(SystemExit) as result: mod.main()
    return result.value.code, json.loads((folder / 'metrics.json').read_text()), folder


def test_partial_level_two_complement_sends_only_missing_same_material(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'
    code, before = run('--gatilho', 'contrato')
    assert code == 3
    states['sample-rev-2'] = 'idle'; sent.clear()
    code, after, folder = complement_round(mod, root, monkeypatch, '--revisor', 'rev-2')
    assert code == 0 and after['review_status'] == 'completa' and after['approval'] is False
    assert sent == ['sample-rev-2'] and after['missing_reviewers'] == []
    assert after['material_manifest'] == before['material_manifest']
    assert after['reviewers']['sample-rev-1'] == before['reviewers']['sample-rev-1']
    assert len(list(root.glob('.herdr/review/*/metrics.json'))) == 1
    assert after['complements'][0]['status'] == 'completa'


@pytest.mark.parametrize('tamper', ['material', 'prior_verdict', 'request'])
def test_complement_refuses_changed_original_material_or_prior_verdict(cli, monkeypatch, tamper):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato')
    folder = next(root.glob('.herdr/review/*/metrics.json')).parent
    if tamper == 'material': target = next((folder / '_snapshot').rglob('readme.txt'))
    elif tamper == 'request': target = folder / 'sample-rev-2/request.md'
    else: target = folder / 'sample-rev-1/verdict.md'
    target.write_text('changed')
    states['sample-rev-2'] = 'idle'; sent.clear()
    code, metrics, _ = complement_round(mod, root, monkeypatch)
    assert code == 1 and not sent and metrics['review_status'] == 'parcial'


def test_complement_busy_remains_partial_and_does_not_send(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato'); sent.clear()
    code, metrics, _ = complement_round(mod, root, monkeypatch)
    assert code == 3 and not sent and metrics['review_status'] == 'parcial'
    assert states['sample-rev-2'] == 'working'


def test_complement_refuses_successful_reviewer_and_legacy_missing_hashes(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato'); sent.clear()
    code, _, folder = complement_round(mod, root, monkeypatch, '--revisor', 'rev-1')
    assert code == 1 and not sent
    path = folder / 'metrics.json'; data = json.loads(path.read_text()); data.pop('material_manifest')
    path.write_text(json.dumps(data))
    code, _, _ = complement_round(mod, root, monkeypatch)
    assert code == 1 and not sent


@pytest.mark.parametrize('tracked', [False, True])
def test_freeze_includes_staged_content_cancelled_by_worktree(tmp_path, tracked):
    import subprocess
    mod = load()
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@localhost')
    path = tmp_path / '.env'
    if tracked: path.write_text('original\n')
    git('add', '.'); git('commit', '--allow-empty', '-m', 'fixture')
    path.write_text('staged-placeholder\n'); git('add', '.env')
    if tracked: path.write_text('original\n')
    else: path.unlink()
    (tmp_path / 'readme').write_text('neutral\n')
    out = tmp_path / 'frozen'; out.mkdir()
    mod.freeze_diff(str(tmp_path), 'HEAD', str(out))
    assert 'staged-placeholder' in (out / 'index.patch').read_text()
    assert '.env' in (out / 'worktree.patch').read_text()
    assert mod.complement.manifest(out)['index.patch']


def test_complement_interrupted_unknown_is_not_resent(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato'); states['sample-rev-2'] = 'idle'
    def crash(*a, **kw): raise OSError('simulated crash at submission')
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', crash)
    code, metrics, folder = complement_round(mod, root, monkeypatch)
    assert code == 1 and metrics['complements'][-1]['status'] == 'in_progress'
    def must_not_send(*a, **kw): raise AssertionError('unknown may not be resent')
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', must_not_send)
    sent.clear()
    code, metrics, _ = complement_round(mod, root, monkeypatch)
    assert code == 1 and not sent and metrics['review_status'] == 'parcial'


def test_complement_never_includes_peer_verdict_in_prompt(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato')
    folder = next(root.glob('.herdr/review/*/metrics.json')).parent
    # Peer verdict content must not be delivered or opened as review context.
    states['sample-rev-2'] = 'idle'
    original = mod.dispatch_and_wait_all
    def checked(prompts, *a, **kw):
        assert list(prompts) == ['sample-rev-2']
        assert 'APPROVE' not in prompts['sample-rev-2']
        assert str(folder / 'sample-rev-1/verdict.md') not in prompts['sample-rev-2']
        return original(prompts, *a, **kw)
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', checked)
    assert complement_round(mod, root, monkeypatch)[0] == 0


def test_concurrent_complement_reservation_refuses_before_delivery(cli, monkeypatch):
    import fcntl
    import os
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato')
    folder = next(root.glob('.herdr/review/*/metrics.json')).parent
    states['sample-rev-2'] = 'idle'; sent.clear()
    fd = os.open(folder / '.complement.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, metrics, _ = complement_round(mod, root, monkeypatch)
    finally:
        os.close(fd)
    assert code == 1 and not sent and metrics['review_status'] == 'parcial'
    assert not metrics.get('complements')


def test_complete_round_cannot_be_complemented_again(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato')
    states['sample-rev-2'] = 'idle'; sent.clear()
    assert complement_round(mod, root, monkeypatch)[0] == 0
    sent.clear()
    code, metrics, _ = complement_round(mod, root, monkeypatch)
    assert code == 1 and not sent and len(metrics['complements']) == 1


@pytest.mark.parametrize('target_kind', ['snapshot', 'request', 'patch'])
def test_material_manifest_is_frozen_before_first_dispatch(cli, monkeypatch, target_kind):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'
    original = mod.dispatch_and_wait_all
    initial = {}
    def changed_during_dispatch(prompts, *args, **kwargs):
        folder = next(root.glob('.herdr/review/*/sample-rev-1/request.md')).parent.parent
        initial.update(mod.complement.manifest(folder))
        output = original(prompts, *args, **kwargs)
        if target_kind == 'snapshot': target = next((folder / '_snapshot').rglob('readme.txt'))
        elif target_kind == 'request': target = folder / 'sample-rev-2/request.md'
        else: target = folder / 'diff.patch'
        target.write_text('changed during first review')
        return output
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', changed_during_dispatch)
    code, metrics = run('--gatilho', 'contrato')
    assert code == 1 and metrics['material_changed'] and metrics['review_status'] == 'falha'
    assert metrics['material_manifest'] == initial
    states['sample-rev-2'] = 'idle'; sent.clear()
    assert complement_round(mod, root, monkeypatch)[0] == 1 and not sent


def test_complement_known_refusal_before_send_does_not_become_unknown(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato')
    folder = next(root.glob('.herdr/review/*/metrics.json')).parent
    frozen = json.loads((folder / 'metrics.json').read_text())['material_manifest']
    states['sample-rev-2'] = 'idle'; sent.clear()
    calls = [frozen, {'changed': 'hash'}]
    monkeypatch.setattr(mod.complement, 'manifest', lambda path: calls.pop(0))
    code, metrics, _ = complement_round(mod, root, monkeypatch)
    assert code == 1 and not sent
    assert metrics['complements'][-1]['status'] == 'refused_before_send'


def test_complement_material_changed_during_delivery_invalidates_coverage(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'; run('--gatilho', 'contrato')
    folder = next(root.glob('.herdr/review/*/metrics.json')).parent
    states['sample-rev-2'] = 'idle'
    original = mod.dispatch_and_wait_all
    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        next((folder / '_snapshot').rglob('readme.txt')).write_text('changed during complement')
        return result
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', mutate)
    code, metrics, _ = complement_round(mod, root, monkeypatch)
    assert code == 1 and metrics['review_status'] == 'falha' and metrics['material_changed']
    assert metrics['approval'] is False and len(metrics['missing_reviewers']) == 2


def test_verdict_changed_after_settlement_cannot_be_coverage(cli, monkeypatch):
    mod, states, sent, _, run, root = cli
    states['sample-rev-2'] = 'working'
    def dispatch(prompts, timeout, required_artifacts, **kwargs):
        name = 'sample-rev-1'; path = Path(required_artifacts[name]); path.write_text('APPROVE')
        published = mod.core._artifact_snapshot(str(path))
        path.write_text('altered after settlement')
        return {name: 'done'}, {name: {'required_artifact': published}}, {}
    monkeypatch.setattr(mod, 'dispatch_and_wait_all', dispatch)
    code, metrics = run('--gatilho', 'contrato')
    assert code == 1 and metrics['reviewers']['sample-rev-1']['status'] == 'artifact_changed'
    assert metrics['review_status'] == 'falha'


@pytest.mark.parametrize('partial_stdout', ['', 'untracked.txt\0'])
def test_freeze_inventory_failure_aborts_before_any_delivery(cli, monkeypatch, partial_stdout):
    import subprocess
    from unittest import mock
    mod, _, sent, _, _, root = cli
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=T', '-c', 'user.email=t@localhost',
                    'commit', '--allow-empty', '-m', 'fixture'], check=True, capture_output=True)
    original = mod.freeze_diff
    calls = []
    def freeze(*args):
        def fail(command, **kwargs):
            calls.append(command)
            assert 'ls-files' in command
            return subprocess.CompletedProcess(command, 1, partial_stdout, 'inventory failed')
        with mock.patch.object(mod.subprocess, 'run', side_effect=fail):
            return original(*args)
    monkeypatch.setattr(mod, 'freeze_diff', freeze)
    monkeypatch.setattr(sys, 'argv', ['herdr-review-dispatch', 'sample', '--description', 'fixture'])
    with pytest.raises(SystemExit) as result: mod.main()
    assert result.value.code == 1 and not sent
    assert len(calls) == 1
    assert not list(root.glob('.herdr/review/*/metrics.json'))
    assert not list(root.glob('.herdr/review/*/request.md'))


def test_round_end_records_the_wake_outcome_and_keeps_the_exit_code(cli, monkeypatch):
    # Pedido 11:50: o fim da rodada acorda o exec; sem HERDR_PANE_ID do exec, so registra "skipped".
    monkeypatch.delenv('HERDR_PANE_ID', raising=False)
    _, states, sent, restored, run, _ = cli
    code, metrics = run('--nivel', '2')
    assert code == 0 and metrics['wake']['status'] in ('skipped', 'refused')


def test_round_end_wakes_the_owner_exec_with_the_round_summary(cli, monkeypatch):
    mod, states, sent, restored, run, _ = cli
    states['sample-exec'] = 'idle'
    monkeypatch.setenv('HERDR_PANE_ID', 'w:px')
    real_info = mod.core.get_agent_info
    monkeypatch.setattr(mod.core, 'get_agent_info',
                        lambda n: {**real_info('sample-rev-1'), 'name': n, 'pane_id': 'w:px'} if n == 'sample-exec' else real_info(n))
    calls = []
    monkeypatch.setattr(mod.core, 'api', lambda *a: calls.append(a))
    code, metrics = run('--nivel', '2')
    assert code == 0 and metrics['wake']['status'] == 'delivered'
    assert calls and calls[-1][:3] == ('agent', 'prompt', 'w:px')
    assert 'rev-1=APPROVE' in calls[-1][3] and 'completa' in calls[-1][3]


def test_the_wake_runs_after_the_space_reservation_is_released(cli, monkeypatch):
    # r204/rev-2: a espera de ate 300 s pelo exec nao pode prender a reserva do space
    mod, states, sent, restored, run, _ = cli
    state = {"open": False, "open_at_wake": None, "calls": 0}
    import contextlib

    @contextlib.contextmanager
    def reservation(scope, core):
        state["open"] = True
        try:
            yield
        finally:
            state["open"] = False
    monkeypatch.setattr(mod.review_scope, "reservation", reservation)

    def fake_wake_round(round_dir, slug, core, coordinator, *, own_pane, publish=None):
        state["open_at_wake"], state["calls"] = state["open"], state["calls"] + 1
        return {"status": "delivered"}
    monkeypatch.setattr(mod.review_wake, "wake_round", fake_wake_round)
    code, _metrics = run('--nivel', '2')
    assert code == 0 and state["calls"] == 1 and state["open_at_wake"] is False
