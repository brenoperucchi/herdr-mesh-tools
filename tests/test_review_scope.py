"""Registered review targets are distinct from agents' home repositories."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import subprocess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from test_review_levels import cli


def repo(path):
    path.mkdir(exist_ok=True)
    subprocess.run(['git', 'init', '--quiet', str(path)], check=True)
    return str(path.resolve())


@pytest.fixture
def scope_env(tmp_path, monkeypatch):
    import _herdr_review_scope as scope
    base = repo(tmp_path / 'base'); target = repo(tmp_path / 'target')
    entry = ('sample', base, ('sample-exec', 'claude', []), None, 'sample',
             {'additional_repositories': [target]})
    owner = dict(name='sample-exec', agent='claude', cwd=base, foreground_cwd=base,
                 agent_session={'value': 'owner-session'}, pane_id='w:p0',
                 terminal_id='terminal-owner', tab_id='w:t', workspace_id='w')
    monkeypatch.setattr(scope, 'entries', lambda: [entry])
    core = SimpleNamespace(get_agent_info=lambda n: dict(owner), herdr_argv=lambda: ['herdr'],
                           project_root=lambda p: scope.git_root(p))
    monkeypatch.setenv('HERDR_MESH_STATE_DIR', str(tmp_path / 'state'))
    return scope, core, base, target, owner


def test_registered_target_keeps_agent_base(scope_env):
    scope, core, base, target, _ = scope_env
    s = scope.resolve('sample', target, base, core)
    assert s['target'] == target and s['base'] == base
    scope.revalidate(s, core)


@pytest.mark.parametrize('case', ['unregistered', 'not_root', 'not_git'])
def test_target_refusals_before_effects(scope_env, case, tmp_path):
    scope, core, base, target, _ = scope_env
    p = tmp_path / 'foreign'; repo(p)
    if case == 'not_root':
        p = Path(target) / 'child'; p.mkdir()
    elif case == 'not_git':
        p = tmp_path / 'plain'; p.mkdir()
    with pytest.raises(RuntimeError): scope.resolve('sample', str(p), base, core)


def test_changed_owner_refuses(scope_env):
    scope, core, base, target, owner = scope_env
    s = scope.resolve('sample', target, base, core)
    owner['agent_session'] = {'value': 'replacement'}
    with pytest.raises(RuntimeError): scope.revalidate(s, core)


def test_space_lock_shared_across_repositories(scope_env):
    scope, core, base, target, _ = scope_env
    a = scope.resolve('sample', target, base, core)
    b = scope.resolve('sample', base, base, core)
    with scope.reservation(a, core):
        with pytest.raises(RuntimeError):
            with scope.reservation(b, core): pass


def test_files_cannot_escape_target(scope_env, tmp_path):
    scope, _, base, target, _ = scope_env
    foreign = Path(base) / 'private'; foreign.write_text('fixture')
    link = Path(target) / 'alias'; link.symlink_to(foreign)
    with pytest.raises(RuntimeError): scope.validate_files(target, [str(link)])


def test_verify_cannot_cross_target(scope_env):
    scope, _, base, target, _ = scope_env
    prior = Path(base) / '.herdr/review/sample-1'; prior.mkdir(parents=True)
    with pytest.raises(RuntimeError): scope.validate_round(target, str(prior), 'sample')


@pytest.fixture
def scoped_cli(cli, monkeypatch):
    mod, states, sent, restored, _, base = cli
    monkeypatch.setenv("HERDR_MESH_STATE_DIR", str(base / "state"))
    import _herdr_review_scope as scope
    repo(base)
    target = Path(repo(base.with_name(base.name + '-target')))
    owner = dict(name='sample-exec', cwd=str(base), foreground_cwd=str(base), agent='codex',
                 agent_session={'value': 'owner'}, pane_id='w:p0', tab_id='w:t', workspace_id='w')
    original = mod.core.get_agent_info
    monkeypatch.setattr(mod.core, 'get_agent_info', lambda n: owner if n=='sample-exec' else original(n))
    monkeypatch.setattr(scope, 'entries', lambda: [('sample', str(base), ('sample-exec','codex',[]), None,
                 'sample', {'additional_repositories': [str(target)]})])
    checked=[]
    original_validate=mod.validate_agent_project
    monkeypatch.setattr(mod, 'validate_agent_project',lambda n,r: (checked.append(r),original_validate(n,r))[1])
    file=target/'sample.txt'; file.write_text('material')
    def run(*extra):
        argv=['herdr-review-dispatch','sample','--repo',str(target),*extra]
        monkeypatch.setattr(sys,'argv',argv)
        with pytest.raises(SystemExit) as result: mod.main()
        return result.value.code
    return mod, states, sent, restored, base, target, file, checked, run


def test_cli_repo_material_and_metrics_at_target(scoped_cli):
    mod, states, sent, restored, base, target, file, checked, run = scoped_cli
    code = run('--description','fixture','--files',str(file))
    assert code==0
    metrics=json.loads(next(target.glob('.herdr/review/*/metrics.json')).read_text())
    assert metrics['review_scope']['target']==str(target)
    assert metrics['review_scope']['base']==str(base)
    assert checked and all(r==str(base) for r in checked)
    assert not list(base.glob('.herdr/review/*'))
    text=next(target.glob('.herdr/review/*/*/request.md')).read_text()
    assert str(target) in text and str(base) in text


def test_repo_complement_reuses_target_and_base_agents(scoped_cli):
    mod, states, sent, restored, base, target, file, checked, run = scoped_cli
    states['sample-rev-2']='working'
    assert run('--description','fixture','--files',str(file))==3
    folder=next(target.glob('.herdr/review/*/metrics.json')).parent
    before=json.loads((folder/'metrics.json').read_text())
    states['sample-rev-2']='idle'; sent.clear()
    assert run('--complementar',str(folder))==0
    after=json.loads((folder/'metrics.json').read_text())
    assert sent==['sample-rev-2'] and after['review_status']=='completa'
    assert before['material_manifest']==after['material_manifest']
    assert after['review_scope']==before['review_scope']
    assert not list(base.glob('.herdr/review/*'))


def test_repo_verify_uses_target_and_refuses_foreign_prior(scoped_cli, monkeypatch):
    mod, states, sent, restored, base, target, file, checked, run = scoped_cli
    folder=target/'.herdr/review/sample-1';folder.mkdir(parents=True)
    (folder/'metrics.json').write_text(json.dumps({'slug':'sample','head_sha':'a'*40}))
    for name in states:
        p=folder/name;p.mkdir();(p/'verdict.md').write_text('prior')
    frozen=[]
    def freeze(cwd, ref, out):
        frozen.append((cwd,ref));(Path(out)/'diff.patch').write_text('fixture')
        return 'b'*40
    monkeypatch.setattr(mod,'freeze_diff',freeze)
    assert run('--verify',str(folder),'--reviewer','rev-1')==0
    assert frozen==[(str(target),'a'*40)]
    assert sent==['sample-rev-1']
    sent.clear()
    foreign=base/'.herdr/review/sample-1';foreign.mkdir(parents=True)
    assert run('--verify',str(foreign))==1 and not sent


def test_repo_cli_refuses_foreign_file_before_restore(scoped_cli):
    mod, states, sent, restored, base, target, file, checked, run = scoped_cli
    foreign=base/'private.txt';foreign.write_text('fixture')
    assert run('--description','fixture','--files',str(foreign))==1
    assert sent==restored==[] and not list(target.glob('.herdr/review/*'))


def test_complement_scope_refusal_is_known_before_send_and_can_resume(scoped_cli,monkeypatch):
    mod, states, sent, restored, base, target, file, checked, run = scoped_cli
    states['sample-rev-2']='working'
    assert run('--description','fixture','--files',str(file))==3
    folder=next(target.glob('.herdr/review/*/metrics.json')).parent
    states['sample-rev-2']='idle';sent.clear()
    owner=mod.core.get_agent_info('sample-exec')
    original=mod.core.ensure_required_agents
    def changed(*a,**kw):
        out=original(*a,**kw);owner['foreground_cwd']=str(target);return out
    monkeypatch.setattr(mod.core,'ensure_required_agents',changed)
    assert run('--complementar',str(folder))==1 and not sent
    metrics=json.loads((folder/'metrics.json').read_text())
    assert metrics['complements'][-1]['status']=='refused_before_send'
    owner['foreground_cwd']=str(base)
    monkeypatch.setattr(mod.core,'ensure_required_agents',original)
    assert run('--complementar',str(folder))==0 and sent==['sample-rev-2']


@pytest.mark.parametrize('directory_swap',[False,True])
def test_files_redirected_during_recovery_never_copy_external(scoped_cli,monkeypatch,directory_swap):
    mod, states, sent, restored, base, target, file, checked, run = scoped_cli
    external=base/'private';external.mkdir();(external/'sample.txt').write_text('private bytes')
    inside=target/'inside';inside.mkdir();(inside/'sample.txt').write_text('public bytes')
    if directory_swap:
        alias=inside/'sample.txt'
    else:
        alias=target/'alias';alias.symlink_to(inside/'sample.txt')
    original=mod.core.ensure_required_agents
    changed=[False]
    def swap(*a,**kw):
        out=original(*a,**kw)
        if not changed[0]:
            changed[0]=True
            if directory_swap:
                inside.rename(target/'old-inside');inside.symlink_to(external,target_is_directory=True)
            else:
                alias.unlink();alias.symlink_to(external/'sample.txt')
        return out
    monkeypatch.setattr(mod.core,'ensure_required_agents',swap)
    assert run('--description','fixture','--files',str(alias))==1
    assert not sent


def test_scope_checked_at_every_prompt_including_retry(monkeypatch):
    import _herdr_dispatch as core
    from unittest import mock
    for retry in (False,True):
        count=[0];sent=[];changed=[False]
        def guard(*a,**kw):
            count[0]+=1
            if count[0]==(3 if retry else 2):changed[0]=True
        def check():
            if changed[0]:raise RuntimeError('coordinator changed')
        def popen(*a,**kw):
            sent.append(a);p=mock.Mock();p.poll.return_value=1
            p.communicate.return_value=(json.dumps({'error':{'code':'agent_prompt_stalled'}}),'')
            return p
        with mock.patch.object(core,'require_can_send',guard), \
             mock.patch.object(core,'get_agent_info',return_value={'agent_status':'idle','pane_id':'w:p1'}), \
             mock.patch.object(core,'pane_looks_busy_with_human_input',return_value=(False,None)), \
             mock.patch.object(core.subprocess,'run',return_value=type('Out',(),{'stdout':''})()), \
             mock.patch.object(core.subprocess,'Popen',popen):
            result,detail,_=core._dispatch_and_wait_all_impl(
                {'sample-rev-1':'read /tmp/.herdr/review/fixture'},30,submission_check=check)
        assert len(sent)==(1 if retry else 0)
        assert result['sample-rev-1']=='preflight_aborted'


def test_scoped_freeze_accepts_stable_alias_and_uses_checked_fd(scope_env,tmp_path,monkeypatch):
    scope, _, base, target, _ = scope_env
    source=Path(target)/'source';source.write_text('safe bytes')
    alias=Path(target)/'alias';alias.symlink_to(source)
    round_dir=tmp_path/'round'
    mapping=scope.freeze_files([str(alias)],str(round_dir),target)
    assert Path(mapping[str(alias)]).read_text()=='safe bytes'
    external=Path(base)/'external';external.write_text('outside')
    original=scope.os.open
    def changed(*a,**kw):
        fd=original(*a,**kw)
        alias.unlink();alias.symlink_to(external)
        return fd
    monkeypatch.setattr(scope.os,'open',changed)
    with pytest.raises(RuntimeError):
        scope.freeze_files([str(alias)],str(tmp_path/'round2'),target)
    assert not (tmp_path/'round2').exists()


def test_default_target_from_registered_invocation_is_explicitly_supported(scope_env):
    scope, core, base, target, _=scope_env
    assert scope.resolve('sample',None,target,core)['target']==target


@pytest.mark.parametrize('case',['removed','duplicate'])
def test_copy_oserror_cleans_round_before_any_submission(scoped_cli,monkeypatch,case):
    mod, states, sent, restored, base, target, file, checked, run = scoped_cli
    original=mod.core.ensure_required_agents
    if case=='removed':
        def remove(*a,**kw):
            out=original(*a,**kw);file.unlink(missing_ok=True);return out
        monkeypatch.setattr(mod.core,'ensure_required_agents',remove)
    files=[str(file)]*(2 if case=='duplicate' else 1)
    assert run('--description','fixture','--files',*files)==1
    assert not sent and not list(target.glob('.herdr/review/*'))
