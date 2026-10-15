"""Disposable, in-memory Herdr lifecycle plus real cross-process flock tests."""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
import copy
import json
import multiprocessing
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import _herdr_dispatch as core
import _herdr_review_groups as groups


def load(name):
    loader = SourceFileLoader(name.replace('-', '_') + '_group_tests', str(Path(core.__file__).parent / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def agent(name, root, pane='w9:p19', tab='w9:tC', kind='claude'):
    return dict(name=name, cwd=str(root), foreground_cwd=str(root), pane_id=pane,
                tab_id=tab, workspace_id='w9', agent=kind, agent_session={'value': 'session-'+name, 'kind':'id'},
                agent_status='idle', interactive_ready=True, revision=2, state_change_seq=4)


class FakeHerdr:
    def __init__(self, root, group):
        self.agents = {group.coordinator_name: agent(group.coordinator_name, root)}
        self.agents['neighbor-exec'] = agent('neighbor-exec', root, 'w9:p1', 'w9:t1')
        self.panes = {a['pane_id']: dict(a) for a in self.agents.values()}
        self.calls = []
        self.next = 20
        self.group = group
        self.artifacts = {}
        self.original_popen = core.subprocess.Popen

    def get(self, name):
        if name not in self.agents:
            raise RuntimeError('agent_not_found: '+name)
        return copy.deepcopy(self.agents[name])

    def api(self, *args):
        if args[:2] == ('agent','get'):
            return {'agent':self.get(args[2])}
        if args[:2] == ('agent','list'):
            return {'agents':list(self.agents.values())}
        if args[:2] == ('pane','get'):
            return {'pane':copy.deepcopy(self.panes[args[2]])}
        if args[:2] == ('agent','prompt'):
            core.guard_delivery(args, self.get(args[2]))
            self.calls.append(args)
            return {'agent':self.get(args[2])}
        self.calls.append(args)
        if args[:2] == ('pane','split'):
            origin = self.panes[args[2]]
            assert '--no-focus' in args
            pane = f'w9:p{self.next}'; self.next += 1
            self.panes[pane] = dict(pane_id=pane, tab_id=origin['tab_id'], workspace_id='w9')
            return {'pane':self.panes[pane]}
        if args[:2] == ('agent','start'):
            name=args[2]; pane=args[args.index('--pane')+1]; kind=args[args.index('--kind')+1]
            info=agent(name, self.agents[self.group.coordinator_name]['cwd'], pane,
                       self.panes[pane]['tab_id'],kind)
            self.agents[name]=info; self.panes[pane]=dict(info)
            return {'agent':info}
        raise AssertionError(args)

    def close(self, info):
        self.calls.append(('pane','close',info['pane_id']))
        del self.agents[info['name']]; del self.panes[info['pane_id']]
        return True, ''

    def popen(self, argv, **kwargs):
        if 'agent' not in argv:
            return self.original_popen(argv, **kwargs)
        offset=argv.index('agent'); operation=argv[offset+1]; name=argv[offset+2]
        assert operation == 'prompt'
        self.calls.append(('submission',name))
        path=self.artifacts[name]
        content='APPROVE\nação necessária: nenhuma\n' if str(path).endswith('verdict.md') else 'Recomendação executável: manter isolamento.\nValidação e próximo passo: teste.\n'
        Path(path).write_text(content)
        proc=mock.Mock()
        proc.poll.return_value=0
        proc.communicate.return_value=(json.dumps({'result':{'agent':self.get(name)}}),'')
        return proc


@pytest.fixture
def env(tmp_path, monkeypatch):
    root=tmp_path/'project'; root.mkdir()
    group=groups.ReviewGroup('sample','custom-coordinator',str(root))
    monkeypatch.setattr(groups,'GROUPS',{'sample':group})
    fake=FakeHerdr(root,group)
    monkeypatch.setattr(core,'get_agent_info',fake.get)
    monkeypatch.setattr(core,'api',fake.api)
    monkeypatch.setattr(core,'pane_looks_busy_with_human_input',lambda *a,**kw:(False,None))
    monkeypatch.setattr(core,'require_can_send',lambda *a,**kw:None)
    boot=load('herdr-bootstrap')
    monkeypatch.setattr(boot,'api',fake.api)
    monkeypatch.setattr(boot,'SPACES',[])
    monkeypatch.setattr(boot,'DRY',False)
    args=SimpleNamespace(with_reviewers=True,with_scout=False,role=[])
    return root,group,fake,boot,args


def test_bootstrap_ask_release_recreate_in_coordinator_tab(env, monkeypatch):
    root,group,fake,boot,args=env
    assert boot.bootstrap_review_group(group,args)==0
    assert set(fake.agents)=={'custom-coordinator','neighbor-exec','sample-rev-1','sample-rev-2'}
    assert fake.get('sample-rev-1')['tab_id']=='w9:tC'
    assert fake.get('sample-rev-2')['tab_id']=='w9:tC'
    assert not (root/'.herdr'/'migration-state.json').exists()
    first_panes={n:fake.get(n)['pane_id'] for n in group.roles()}
    boot.bootstrap_review_group(group,args)
    assert first_panes=={n:fake.get(n)['pane_id'] for n in group.roles()}
    assert len([c for c in fake.calls if c[:2]==('pane','split')])==2

    ask=load('herdr-ask'); q=root/'question.md'; q.write_text('Validate this isolated fixture.')
    monkeypatch.chdir(root)
    monkeypatch.setattr(core,'require_herdr_pane',lambda:None)
    monkeypatch.setattr(ask,'runtime_profile_evidence',lambda *a,**kw:{'observed':False})
    def dispatch(prompts,timeout,required_artifacts,**kwargs):
        fake.artifacts=required_artifacts
        with mock.patch.object(core.subprocess,'Popen',fake.popen):
            return core.dispatch_and_wait_all(prompts,timeout,required_artifacts,**kwargs)
    monkeypatch.setattr(ask,'dispatch_and_wait_all',dispatch)
    monkeypatch.setattr(sys,'argv',['herdr-ask','sample','--question-file',str(q)])
    with pytest.raises(SystemExit) as result:
        ask.main()
    assert result.value.code==0
    metrics=json.loads((root/'.herdr/ask/sample-1/metrics.json').read_text())
    assert metrics['group_binding']['coordinator']['name']=='custom-coordinator'
    assert all(r['status'] in ('idle','done') for r in metrics['reviewers'].values())
    release=load('herdr-release'); monkeypatch.setattr(release,'fechar',fake.close)
    monkeypatch.setattr(core,'classify_composition',lambda p:('vazia','',''))
    out=release.liberar('sample',list(fake.agents.values()),'reviewers',False,False)
    assert [i['acao'] for i in out]==['fechado','fechado']
    assert set(fake.agents)=={'custom-coordinator','neighbor-exec'}
    boot.bootstrap_review_group(group,args)
    assert all(fake.get(n)['tab_id']=='w9:tC' for n in group.roles())
    assert all(fake.get(n)['pane_id']!=first_panes[n] for n in group.roles())
    assert fake.get('neighbor-exec')['pane_id']=='w9:p1'


def test_dry_run_never_splits_or_writes_migration(env, monkeypatch):
    root,group,fake,boot,args=env
    monkeypatch.setattr(boot,'DRY',True)
    assert boot.bootstrap_review_group(group,args)==0
    assert fake.calls==[]
    assert not (root/'.herdr').exists()


@pytest.mark.parametrize('field,value',[('tab_id','w9:t1'),('cwd','/elsewhere'),
                                       ('foreground_cwd','/elsewhere'),('agent','codex'),('agent_session',None),('interactive_ready',False)])
def test_existing_wrong_member_refuses_before_splitting(env,field,value):
    root,group,fake,boot,args=env
    info=agent('sample-rev-2',root,'w9:p8',kind='claude');info[field]=value
    fake.agents[info['name']]=info
    with pytest.raises(RuntimeError): boot.bootstrap_review_group(group,args)
    assert fake.calls==[]


def test_binding_changed_after_split_never_starts_agent(env):
    root,group,fake,boot,args=env
    original=boot.api
    def api(*a):
        result=original(*a)
        if a[:2]==('pane','split'):
            fake.agents[group.coordinator_name]['agent_session']={'value':'changed'}
        return result
    boot.api=api
    with pytest.raises(RuntimeError,match='criação parcial'):
        boot.bootstrap_review_group(group,args)
    assert not any(c[:2]==('agent','start') for c in fake.calls)


def test_single_role_request_creates_only_that_role(env):
    _,group,fake,boot,args=env
    args.with_reviewers=False;args.role=['rev-1']
    boot.bootstrap_review_group(group,args)
    assert 'sample-rev-1' in fake.agents
    assert 'sample-rev-2' not in fake.agents


def test_unregistered_scout_refuses_without_changes(env):
    _,group,fake,boot,args=env;args.with_scout=True
    with pytest.raises(RuntimeError,match='não declara scout'):
        boot.bootstrap_review_group(group,args)
    assert fake.calls==[]


def test_force_cannot_close_working_group_member(env,monkeypatch):
    _,group,fake,boot,args=env;boot.bootstrap_review_group(group,args)
    fake.agents['sample-rev-1']['agent_status']='working'
    release=load('herdr-release');monkeypatch.setattr(release,'fechar',fake.close)
    monkeypatch.setattr(core,'classify_composition',lambda p:('vazia','',''))
    result=release.liberar('sample',list(fake.agents.values()),'reviewers',True,False)
    assert next(i for i in result if i['name']=='sample-rev-1')['acao']=='recusado'
    assert 'sample-rev-1' in fake.agents


def test_legacy_cannot_adopt_foreign_root_or_group_tab(env):
    root,group,fake,boot,_=env
    anonymous=agent('',root,'w9:p30')
    all_agents=[*fake.agents.values(),anonymous]
    assert boot.find_unnamed_role_base(all_agents,'w9','w9:t1',expected_cwd='/different') is None
    # Even the same project cannot make the group's tab a legacy scout tab.
    assert boot.find_unnamed_role_base(all_agents,'w9','w9:t1',expected_cwd=str(root)) is None
    legit=agent('',root,'w9:p31','w9:tScout')
    assert boot.find_unnamed_role_base([*fake.agents.values(),legit],'w9','w9:t1',expected_cwd=str(root))==legit


def test_custom_coordinator_reaches_all_prompt_text(env):
    root,group,_,_,_=env
    prompt=core.role_reinforcement_prompt('sample-scout','sample',str(root),[group.coordinator_name])
    assert 'sample-exec' not in prompt
    assert 'custom-coordinator' in prompt
    assert core._infer_role(group.coordinator_name)=='exec'


def test_registry_rejects_role_coordinator_collision(monkeypatch,tmp_path):
    monkeypatch.setattr(groups,'GROUPS',{'g':groups.ReviewGroup('g','g-rev-1',str(tmp_path))})
    with pytest.raises(RuntimeError,match='coordenador'):
        groups.validate_registry()


def _contend(group,pipe):
    runtime=SimpleNamespace(herdr_argv=lambda:['herdr','--session','fake'],DISPATCH_LOCK_WAIT_S=0)
    try:
        with groups.group_lock(group,runtime): pipe.send('acquired')
    except RuntimeError: pipe.send('busy')
    finally: pipe.close()


def test_group_lock_is_shared_across_processes_and_independent_of_tmpdir(env,monkeypatch):
    _,group,_,_,_=env
    runtime=SimpleNamespace(herdr_argv=lambda:['herdr','--session','fake'],DISPATCH_LOCK_WAIT_S=0)
    ctx=multiprocessing.get_context('fork'); parent,child=ctx.Pipe()
    with groups.group_lock(group,runtime):
        monkeypatch.setenv('TMPDIR','/different-temp-location')
        proc=ctx.Process(target=_contend,args=(group,child));proc.start()
        assert parent.poll(3)
        assert parent.recv()=='busy'
        proc.join(3); assert proc.exitcode==0
    # Holding a neighboring group's lock does not block this group.
    other=groups.ReviewGroup('other','another-coordinator',group.project_root)
    with groups.group_lock(other,runtime),groups.group_lock(group,runtime): pass


def test_missing_coordinator_never_creates_a_replacement(env):
    _,group,fake,boot,args=env
    del fake.agents[group.coordinator_name]
    with pytest.raises(RuntimeError,match='agent_not_found'):
        boot.bootstrap_review_group(group,args)
    assert fake.calls==[]


def test_legacy_alias_conflict_does_not_write_migration(env):
    root,group,fake,boot,args=env
    fake.agents['sample-rev']=agent('sample-rev',root,'w9:p88',kind='codex')
    with pytest.raises(RuntimeError,match='legacy conflitante'):
        boot.bootstrap_review_group(group,args)
    assert fake.calls==[]
    assert not (root/'.herdr').exists()


def test_group_accepts_realpath_alias_and_git_subdirectory(env):
    root,group,fake,_,_=env
    import subprocess
    subprocess.run(['git','init','--quiet',str(root)],check=True)
    child=root/'child';child.mkdir()
    alias=root.parent/'alias';alias.symlink_to(root,target_is_directory=True)
    fake.agents[group.coordinator_name]['cwd']=str(alias)
    fake.agents[group.coordinator_name]['foreground_cwd']=str(child)
    assert groups.resolve_binding(group,core,str(alias))['root']==str(root)


def test_member_change_during_release_guard_prevents_close(env,monkeypatch):
    _,group,fake,boot,args=env;boot.bootstrap_review_group(group,args)
    release=load('herdr-release');monkeypatch.setattr(release,'fechar',fake.close)
    monkeypatch.setattr(core,'classify_composition',lambda p:('vazia','',''))
    def moved(name,**kw):
        fake.agents[name]['agent_session']={'value':'a-replacement-session'}
    monkeypatch.setattr(core,'require_can_send',moved)
    out=release.liberar('sample',list(fake.agents.values()),'reviewers',False,False)
    assert all(i['acao']=='recusado' for i in out)
    assert not any(c[:2]==('pane','close') for c in fake.calls)


def test_artifact_without_terminal_round_metrics_cannot_be_released(env,monkeypatch):
    root,group,fake,boot,args=env;boot.bootstrap_review_group(group,args)
    for name in group.roles():
        folder=root/'.herdr/ask/sample-1'/name;folder.mkdir(parents=True)
        (folder/'answer.md').write_text('not yet published as a completed round')
    release=load('herdr-release');monkeypatch.setattr(release,'fechar',fake.close)
    monkeypatch.setattr(core,'classify_composition',lambda p:('vazia','',''))
    out=release.liberar('sample',list(fake.agents.values()),'reviewers',True,False)
    assert all(i['acao']=='recusado' for i in out)


def _bootstrap_worker(boot,group,args,pipe):
    try:
        boot.bootstrap_review_group(group,args);pipe.send('ok')
    except Exception as exc: pipe.send(repr(exc))
    finally: pipe.close()


def test_concurrent_bootstraps_create_one_pair_only(env):
    _,group,fake,boot,args=env
    ctx=multiprocessing.get_context('fork')
    with ctx.Manager() as manager:
        fake.agents=manager.dict(fake.agents);fake.panes=manager.dict(fake.panes)
        fake.calls=manager.list()
        pipes=[];workers=[]
        for _ in range(2):
            parent,child=ctx.Pipe();pipes.append(parent)
            p=ctx.Process(target=_bootstrap_worker,args=(boot,group,args,child));p.start();workers.append(p)
        for parent in pipes:
            assert parent.poll(8), 'bootstrap child deadlocked'
            assert parent.recv()=='ok'
        for p in workers: p.join(5);assert p.exitcode==0
        assert len([c for c in fake.calls if c[:2]==('pane','split')])==2
        assert all(fake.agents[n]['tab_id']=='w9:tC' for n in group.roles())


@pytest.mark.parametrize('reviewer,expected',[('rev-1','sample-rev-1'),('rev-2','sample-rev-2'),(None,'sample-rev-2')])
def test_verify_main_provisions_only_selected_role(env,monkeypatch,reviewer,expected):
    root,group,fake,boot,args=env
    previous=root/'.herdr/review/sample-1';previous.mkdir(parents=True)
    (previous/'metrics.json').write_text(json.dumps({'slug':'sample','head_sha':'a'*40}))
    for name in group.roles():
        folder=previous/name;folder.mkdir();(folder/'verdict.md').write_text('prior verdict')
    review=load('herdr-review-dispatch');observed=[]
    def recover(slug,cwd,names):
        observed.extend(names)
        selected=SimpleNamespace(with_reviewers=False,with_scout=False,
                                 role=[group.roles()[n][0] for n in names])
        boot.bootstrap_review_group(group,selected)
        return {'attempted':True,'missing_before':list(names),'restored':list(names)}
    monkeypatch.setattr(core,'ensure_required_agents',recover)
    monkeypatch.setattr(core,'require_herdr_pane',lambda:None)
    monkeypatch.setattr(review,'runtime_profile_evidence',lambda *a,**kw:{'observed':False})
    def freeze(cwd,base,out):
        (Path(out)/'diff.patch').write_text('synthetic diff')
        return 'newhead'
    monkeypatch.setattr(review,'freeze_diff',freeze)
    def dispatch(prompts,timeout,required_artifacts,**kwargs):
        fake.artifacts=required_artifacts
        with mock.patch.object(core.subprocess,'Popen',fake.popen):
            return core.dispatch_and_wait_all(prompts,timeout,required_artifacts,**kwargs)
    monkeypatch.setattr(review,'dispatch_and_wait_all',dispatch)
    monkeypatch.chdir(root)
    argv=['herdr-review-dispatch','sample','--verify',str(previous)]
    if reviewer:argv+=['--reviewer',reviewer]
    monkeypatch.setattr(sys,'argv',argv)
    with pytest.raises(SystemExit) as result:review.main()
    assert result.value.code==0
    assert observed==[expected]
    assert set(fake.agents)=={'custom-coordinator','neighbor-exec',expected}
    assert [c for c in fake.calls if c[0]=='submission']==[('submission',expected)]


def test_invalid_verify_does_not_provision_any_role(env,monkeypatch):
    root,_,fake,_,_=env
    review=load('herdr-review-dispatch')
    monkeypatch.chdir(root);monkeypatch.setattr(core,'require_herdr_pane',lambda:None)
    monkeypatch.setattr(sys,'argv',['herdr-review-dispatch','sample','--verify',str(root/'.herdr/review/missing')])
    with mock.patch.object(core,'ensure_required_agents') as recover,pytest.raises(SystemExit):
        review.main()
    recover.assert_not_called()
    assert fake.calls==[]


def test_rev2_only_recovery_splits_below_existing_rev1(env):
    _,group,fake,boot,args=env
    args.with_reviewers=False;args.role=['rev-1'];boot.bootstrap_review_group(group,args)
    source=fake.get('sample-rev-1')['pane_id']
    args.role=['rev-2'];boot.bootstrap_review_group(group,args)
    last=[c for c in fake.calls if c[:2]==('pane','split')][-1]
    assert last[2]==source
    assert last[last.index('--direction')+1]=='down'


def test_refused_hydration_preserves_created_role_and_is_nonfatal(env,monkeypatch,capsys):
    _,group,fake,boot,args=env
    monkeypatch.setattr(core,'require_can_send',mock.Mock(side_effect=core.DeliveryRefusedError('código 1')))
    assert boot.bootstrap_review_group(group,args)==0
    assert all(n in fake.agents for n in group.roles())
    first_calls=len(fake.calls)
    output=capsys.readouterr().out
    assert 'hidratação recusada' in output and 'hydrated=false' in output
    assert 'criação parcial' not in output
    assert not any(c[:2]==('agent','prompt') for c in fake.calls)
    assert boot.bootstrap_review_group(group,args)==0
    assert len(fake.calls)==first_calls


def test_custom_coordinator_does_not_rewrite_historical_agent_names(env,monkeypatch):
    root,_,_,_,_=env
    monkeypatch.setattr(groups,'GROUPS',{'bridge':groups.ReviewGroup('bridge','acervo-bia',str(root))})
    prompt=core.role_reinforcement_prompt('bridge-scout','bridge',str(root),['acervo-bia'])
    assert 'claude-bridge-exec' in prompt
    assert 'claude-acervo-bia' not in prompt
    assert 'acervo-bia' in prompt


def test_real_group_registry_has_no_collision_with_real_spaces():
    boot=load('herdr-bootstrap')
    groups.validate_registry(boot.SPACES)


def test_future_registry_collision_with_spaces_is_detected(tmp_path,monkeypatch):
    boot=load('herdr-bootstrap')
    entry=boot.SPACES[0];slug=entry[4] if len(entry)>4 else entry[0].lower()
    monkeypatch.setattr(groups,'GROUPS',{slug:groups.ReviewGroup(slug,f'{slug}-exec',str(tmp_path))})
    with pytest.raises(RuntimeError,match='colisão'):
        groups.validate_registry(boot.SPACES)


def test_late_runtime_error_is_not_classified_as_preflight(env,monkeypatch):
    def sent_then_failed(*a,**kw):
        core.subprocess.Popen(['synthetic-submission'])
        raise RuntimeError('late transport failure')
    monkeypatch.setattr(core,'_dispatch_and_wait_all_impl',sent_then_failed)
    with mock.patch.object(core.subprocess,'Popen') as sent:
        with pytest.raises(RuntimeError) as result:
            core.dispatch_and_wait_all({'unregistered-target':'test'},30)
    assert not isinstance(result.value,core.DispatchPreflightError)
    sent.assert_called_once()


def test_group_reservation_failure_remains_a_preflight_error(env,monkeypatch):
    _,group,_,boot,args=env;boot.bootstrap_review_group(group,args)
    monkeypatch.setattr(core,'DISPATCH_LOCK_WAIT_S',0)
    with groups.group_lock(group,core),mock.patch.object(core.subprocess,'Popen') as sent:
        with pytest.raises(core.DispatchPreflightError,match='andamento'):
            core.dispatch_and_wait_all({'sample-rev-1':'test'},30)
    sent.assert_not_called()


def test_binding_failure_inside_hydration_is_operational(env,monkeypatch,capsys):
    _,group,fake,boot,args=env
    original=groups.validate_member
    def validate(*a,**kw):
        if sys._getframe(1).f_code.co_name == 'guard_delivery':
            raise RuntimeError('binding changed inside guard')
        return original(*a,**kw)
    monkeypatch.setattr(groups,'validate_member',validate)
    with pytest.raises(RuntimeError,match='criação parcial.*binding changed'):
        boot.bootstrap_review_group(group,args)
    assert 'hydrated=false' not in capsys.readouterr().out
    assert not any(c[:2]==('agent','prompt') for c in fake.calls)


@pytest.mark.parametrize('command',['herdr-ask','herdr-review-dispatch'])
def test_main_group_round_lock_failure_is_clean(env,monkeypatch,capsys,command):
    root,group,fake,boot,args=env;boot.bootstrap_review_group(group,args)
    module=load(command);monkeypatch.chdir(root)
    monkeypatch.setattr(core,'require_herdr_pane',lambda:None)
    monkeypatch.setattr(core,'DISPATCH_LOCK_WAIT_S',0)
    question=root/'question.md';question.write_text('Test lock refusal.')
    argv=[command,'sample']
    if command=='herdr-ask':argv+=['--question-file',str(question)]
    else:argv+=['--description','Test lock refusal.']
    monkeypatch.setattr(sys,'argv',argv)
    with groups.group_lock(group,core),pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code==1
    assert 'erro: grupo sample: rodada ou recuperação em andamento' in capsys.readouterr().err
    assert not list((root/'.herdr').glob('**/request.md'))
    assert not any(c[0]=='submission' for c in fake.calls)
