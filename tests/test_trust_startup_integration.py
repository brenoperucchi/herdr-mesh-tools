"""All startup/config integration uses a synthetic HOME, fake transport."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin'))
import _herdr_trust as trust
import _herdr_startup as startup

MENU=(Path(__file__).parent/'fixtures/startup/codex-0.164-new-conversation.txt').read_text()
CLAUDE='Do you trust this folder?\n  1. Yes, I trust this folder\n› 2. No, exit'


def load(name):
    path=Path(trust.__file__).with_name(name)
    loader=SourceFileLoader(name.replace('-','_')+'_trust_integration',str(path))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    module=importlib.util.module_from_spec(spec);loader.exec_module(module)
    return module


@pytest.fixture
def launch(tmp_path,monkeypatch):
    home=tmp_path/'home';project=home/'Devs/demo';project.mkdir(parents=True)
    monkeypatch.setenv('HOME',str(home));monkeypatch.setenv('CODEX_HOME',str(home/'.codex'))
    monkeypatch.setenv('HERDR_RENAME_HOME',str(home))
    store=trust.TrustStore(home=home,registered=[project])
    monkeypatch.setattr(trust,'git_root',lambda p:None)
    monkeypatch.setattr(trust,'remote_warning',lambda p:None)
    boot=load('herdr-bootstrap');monkeypatch.setattr(boot,'DRY',False)
    events=[];screen=[''];kind=['codex'];status=['idle']
    info={'pane_id':'w:p2','terminal_id':'term-new','name':'sample-rev-1','agent':'codex',
          'agent_status':'idle','cwd':str(project),'foreground_cwd':str(project),
          'revision':2,'state_change_seq':1,'agent_session':None}
    def get(target):
        return {**info,'agent':kind[0],'agent_status':status[0]}
    monkeypatch.setattr(boot.core,'get_agent_info',get)
    def ensure(directory,agent_kind,**kw):
        events.append('trust')
        return store.ensure_trusted(directory,agent_kind)
    monkeypatch.setattr(boot.trust,'ensure_trusted',ensure)
    monkeypatch.setattr(startup,'read_detection',lambda runtime,pane:screen[0])
    def key(runtime,pane):
        events.append('enter');screen[0]='';status[0]='idle';return True
    monkeypatch.setattr(startup,'_send_enter',key)
    def run(*args):
        if args[:2]==('agent','start'):
            events.append('start')
            path=store.config_path(kind[0])
            data=tomllib.loads(path.read_text()) if kind[0]=='codex' else json.loads(path.read_text())
            assert store.trusted(data,project,kind[0]),'trust must precede native start'
        elif args[:2]==('agent','prompt'):
            assert not screen[0],'must not hydrate before dialog cleared'
            events.append('prompt')
        return {}
    monkeypatch.setattr(boot,'run',run)
    monkeypatch.setattr(boot.core,'_wait_restored_agent',lambda *a,**kw:events.append('wait') or get('w:p2'))
    return SimpleNamespace(home=home,project=project,store=store,boot=boot,events=events,screen=screen,
                           kind=kind,status=status,info=info,run=run)


@pytest.mark.parametrize('kind',['codex','claude'])
def test_trust_precedes_native_start_and_hydration(launch,kind):
    env=launch;env.kind[0]=kind
    env.boot.start_agent_with_role('w:p2','sample-rev-1',kind,[],'sample',str(env.project),[])
    assert env.events==['trust','start','prompt']


@pytest.mark.parametrize('native_status',['blocked','idle'])
def test_new_pane_dialog_confirmed_once_before_hydration_even_if_cli_ready(launch,native_status):
    env=launch;env.screen[0]=MENU;env.status[0]=native_status
    receipt=startup.NewPane('w:p2','term-new',str(env.project),startup.time.monotonic())
    env.boot.start_agent_with_role('w:p2','sample-rev-1','codex',[],'sample',str(env.project),[],new_pane=receipt)
    assert env.events==['trust','start','enter','wait','prompt'] and receipt.consumed


def test_agent_not_ready_worktree_dialog_is_resolved_only_with_new_receipt(launch,monkeypatch):
    env=launch;env.screen[0]=MENU;env.status[0]='blocked'
    def run(*args):
        if args[:2]==('agent','start'):
            env.run(*args);raise RuntimeError('agent_not_ready')
        return env.run(*args)
    monkeypatch.setattr(env.boot,'run',run)
    receipt=startup.NewPane('w:p2','term-new',str(env.project),startup.time.monotonic())
    env.boot.start_agent_with_role('w:p2','sample-rev-1','codex',[],'sample',str(env.project),[],new_pane=receipt)
    assert env.events==['trust','start','enter','wait','prompt']


def test_existing_pane_never_confirms_or_hydrates_over_menu(launch):
    env=launch;env.screen[0]=MENU
    with pytest.raises(startup.StartupDialogRefused,match='w:p2'):
        env.boot.start_agent_with_role('w:p2','sample-rev-1','codex',[],'sample',str(env.project),[])
    assert env.events==['trust','start']


def test_claude_no_exit_never_receives_enter_or_hydration(launch):
    env=launch;env.kind[0]='claude';env.screen[0]=CLAUDE;env.status[0]='blocked'
    receipt=startup.NewPane('w:p2','term-new',str(env.project),startup.time.monotonic())
    with pytest.raises(startup.StartupDialogRefused,match='No, exit'):
        env.boot.start_agent_with_role('w:p2','sample-rev-1','claude',[],'sample',str(env.project),[],new_pane=receipt)
    assert env.events==['trust','start'] and not receipt.consumed


def test_bootstrap_dry_run_never_updates_trust_or_inspects_pane(launch,monkeypatch):
    env=launch;monkeypatch.setattr(env.boot,'DRY',True)
    monkeypatch.setattr(env.boot,'run',lambda *a:None)
    monkeypatch.setattr(startup,'finish_start',lambda *a,**k:pytest.fail('dry-run inspected pane'))
    env.boot.start_agent_with_role('w:p2','sample-rev-1','codex',[],'sample',str(env.project),[])
    assert not env.events and not env.store.config_path('codex').exists()


def test_recovery_existing_blocked_claude_reports_pane_text_without_bootstrap(launch,monkeypatch):
    env=launch;env.kind[0]='claude';env.status[0]='blocked';env.screen[0]=CLAUDE
    monkeypatch.setattr(env.boot.core.review_groups,'get_group',lambda slug:None)
    monkeypatch.setattr(env.boot.core.tempfile,'gettempdir',lambda:str(env.home))
    monkeypatch.setattr(env.boot.core.subprocess,'run',lambda *a,**k:pytest.fail('recovery bootstrapped blocked Claude'))
    with pytest.raises(env.boot.core.AgentRestoreError,match='No, exit') as exc:
        env.boot.core.ensure_required_agents('sample',str(env.project),['sample-rev-1'])
    assert 'w:p2' in str(exc.value) and not env.events


def test_rename_trusts_new_registered_directory_but_never_confirms_existing_pane(launch,monkeypatch):
    env=launch;env.kind[0]='claude';env.screen[0]=CLAUDE;env.status[0]='blocked'
    rename=load('herdr-rename-space');calls=[]
    monkeypatch.setattr(rename.time,'sleep',lambda *a:None)
    monkeypatch.setattr(rename.core,'get_agent_info',lambda target:{**env.info,'agent':'claude','agent_status':'blocked'})
    def run(command,**kwargs):
        calls.append(command)
        if 'start' in command:return subprocess.CompletedProcess(command,1,'','agent_not_ready')
        return subprocess.CompletedProcess(command,0,'','')
    monkeypatch.setattr(rename.subprocess,'run',run)
    rs=SimpleNamespace(start_args=lambda *a:[],tratar_dialogos=lambda *a:[],tela=lambda pane:CLAUDE)
    ag={'pane':'w:p2','novo_nome':'sample-rev-1','kind':'claude','sessao':'s','model':'m','effort':'e','argv':[]}
    result=rename.religar(SimpleNamespace(),{'agentes':[ag]},rs,str(env.project))[0]
    assert not result['ok'] and 'No, exit' in result['erro'] and 'w:p2' in result['erro']
    assert sum('start' in command for command in calls)==1
    assert env.events==['trust'] and env.store.config_path('claude').exists()


def test_add_space_trust_occurs_after_registration_and_before_bootstrap(launch,monkeypatch):
    env=launch;add=load('herdr-add-space')
    table=env.home/'bootstrap';table.write_text('SPACES = []\n')
    monkeypatch.setattr(add,'BOOTSTRAP',str(table))
    monkeypatch.setattr(add,'categorias_validas',lambda:['test'])
    monkeypatch.setattr(add,'foco_do_guardian',lambda:None)
    monkeypatch.setattr(add,'registrar_inventario',lambda *a:None)
    monkeypatch.setattr(add,'registrar_guardian',lambda *a:None)
    monkeypatch.setattr(add,'atualizar_catalogo',lambda:None)
    monkeypatch.setattr(add,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,'{"result":{"workspaces":[]}}',''))
    original_ensure=trust.ensure_trusted
    def ensure(directory,kind,**kwargs):
        assert str(env.project) in str(trust.registered_directories(table,env.home))
        return env.store.ensure_trusted(directory,kind)
    monkeypatch.setattr(add.trust,'ensure_trusted',ensure)
    def run(command,**kwargs):
        if command[:2]==[sys.executable,str(table)]:
            assert env.store.trusted(json.loads(env.store.config_path('claude').read_text()),env.project,'claude')
            env.events.append('bootstrap')
        return subprocess.CompletedProcess(command,0,'','')
    monkeypatch.setattr(add.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['herdr-add-space','sample',str(env.project),'--no-scout','--lean',
                                   '--categoria','test','--no-git','--no-openboard','--no-board'])
    assert add.main()==0 and env.events==['bootstrap']


@pytest.mark.parametrize('new_workspace',[False,True])
def test_bootstrap_receipt_only_for_fresh_root_split_and_scout_panes(launch,monkeypatch,new_workspace):
    env=launch;boot=env.boot;root=str(env.project)
    entry=('sample',root,('sample-exec','codex',[]),('sample-scout','codex',[]),'sample',{'lean':False})
    monkeypatch.setattr(boot,'SPACES',[entry])
    monkeypatch.setattr(boot.core,'project_root',lambda p:root)
    monkeypatch.setattr(boot.migration,'space_gate_message',lambda *a:None)
    monkeypatch.setattr(boot.migration,'read_migration_state',lambda p:{'phase':'migrated'})
    monkeypatch.setattr(boot,'build_rev_agents',lambda *a:[('sample-rev-1','codex',[]),('sample-rev-2','claude',[])])
    monkeypatch.setattr(boot,'find_unnamed_reviewer_base',lambda *a,**k:None)
    monkeypatch.setattr(boot,'find_unnamed_role_base',lambda *a,**k:None)
    monkeypatch.setattr(boot,'ensure_notify_watchers',lambda *a:None)
    created=set();issued=[];started=[];counter=[1]
    root_record={'pane_id':'w:p1','tab_id':'w:t1','workspace_id':'w'}
    def api(*args):
        if args==('workspace','list'):return {'workspaces':[] if new_workspace else [{'label':'sample','workspace_id':'w'}]}
        if args==('pane','list'):return {'panes':[] if new_workspace else [root_record]}
        if args==('agent','list'):return {'agents':[]}
        raise AssertionError(args)
    monkeypatch.setattr(boot,'api',api)
    def run(*args):
        if args[:2]==('workspace','create'):
            created.add('w:p1');return {'workspace':{'workspace_id':'w'},'root_pane':root_record}
        if args[:2] in (('pane','split'),('tab','create')):
            counter[0]+=1;pane=f'w:p{counter[0]}';created.add(pane)
            return {'pane':{'pane_id':pane}} if args[0]=='pane' else {'root_pane':{'pane_id':pane}}
        raise AssertionError(args)
    monkeypatch.setattr(boot,'run',run)
    def receipt(runtime,record,cwd):
        assert record['pane_id'] in created,'existing pane received creation privilege'
        issued.append(record['pane_id'])
        return startup.NewPane(record['pane_id'],'term-'+record['pane_id'],cwd,startup.time.monotonic())
    monkeypatch.setattr(startup,'created_pane_receipt',receipt)
    def start(pane,name,kind,extra,slug,cwd,siblings,**kwargs):
        token=kwargs.get('new_pane')
        assert (token is not None)==(pane in created)
        started.append((name,pane))
    monkeypatch.setattr(boot,'start_agent_with_role',start)
    monkeypatch.setattr(sys,'argv',['herdr-bootstrap','--slug','sample'])
    assert boot.main()==0
    assert {name for name,_ in started}=={'sample-exec','sample-rev-1','sample-rev-2','sample-scout'}
    assert set(issued)==created


def test_codex_subtable_does_not_capture_trust_field(launch):
    env=launch
    path=env.store.config_path('codex');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('[projects.'+json.dumps(str(env.project))+'.notes]\nx=1\n')
    result=env.store.ensure_trusted(env.project,'codex')
    assert result['status']=='trusted'
    document=tomllib.loads(path.read_text())
    assert document['projects'][str(env.project)]=={'notes':{'x':1},'trust_level':'trusted'}


def test_invalid_metadata_revalidation_is_refused_without_traceback(launch,monkeypatch):
    env=launch;env.screen[0]=MENU;observations=[{**env.info},None]
    monkeypatch.setattr(env.boot.core,'get_agent_info',lambda target:observations.pop(0))
    receipt=startup.NewPane('w:p2','term-new',str(env.project),startup.time.monotonic())
    runtime=env.boot.startup_runtime()
    assert startup.confirm_new_conversation_dialog(runtime,receipt,name='sample-rev-1',kind='codex') is False
    assert not receipt.consumed and not env.events


def test_empty_existing_claude_config_is_not_treated_as_absent(launch):
    env=launch;path=env.store.config_path('claude');path.write_bytes(b'')
    result=env.store.ensure_trusted(env.project,'claude')
    assert result['status']=='warning' and path.read_bytes()==b''


def test_unconfirmed_claude_identity_still_reports_screen_without_key(launch,monkeypatch):
    env=launch;env.kind[0]='claude';env.screen[0]=CLAUDE
    monkeypatch.setattr(env.boot.core,'get_agent_info',lambda target:{**env.info,'agent':None})
    with pytest.raises(startup.StartupDialogRefused,match='No, exit'):
        startup.finish_start(env.boot.startup_runtime(),None,pane='w:p2',name='sample-rev-1',kind='claude')
    assert not env.events


def test_confirmed_startup_waits_for_ui_settle_without_second_enter(launch,monkeypatch):
    env=launch;core=load('_herdr_dispatch.py');observations=[{**env.info,'agent_status':'blocked'},dict(env.info)]
    monkeypatch.setattr(core,'get_agent_info',lambda target:observations.pop(0))
    sleeps=[];monkeypatch.setattr(core.time,'sleep',lambda seconds:sleeps.append(seconds))
    monkeypatch.setattr(startup,'_send_enter',lambda *a:pytest.fail('wait retried Enter'))
    assert core._wait_restored_agent('sample-rev-1',startup_confirmed=True)['agent_status']=='idle'
    assert sleeps==[0.1]

@pytest.mark.parametrize('screen',[CLAUDE,MENU])
def test_bulk_bootstrap_continues_after_dialog_and_reports_failure(launch,monkeypatch,capsys,screen):
    env=launch;boot=env.boot;root=str(env.project)
    monkeypatch.setattr(boot,'SPACES',[(slug,root,(slug+'-exec','claude' if screen==CLAUDE else 'codex',[]),None,slug,{'lean':True}) for slug in ['alpha','beta']])
    monkeypatch.setattr(boot.core,'project_root',lambda p:root)
    monkeypatch.setattr(boot.migration,'space_gate_message',lambda *a:None)
    monkeypatch.setattr(boot,'find_unnamed_reviewer_base',lambda *a,**k:None)
    monkeypatch.setattr(boot,'ensure_notify_watchers',lambda *a:None)
    records=[{'pane_id':f'{slug}:p1','tab_id':f'{slug}:t1','workspace_id':slug} for slug in ['alpha','beta']]
    def api(*args):
        if args==('workspace','list'):return {'workspaces':[{'label':slug,'workspace_id':slug} for slug in ['alpha','beta']]}
        if args==('pane','list'):return {'panes':records}
        if args==('agent','list'):return {'agents':[]}
        raise AssertionError(args)
    monkeypatch.setattr(boot,'api',api)
    starts=[];prompts=[]
    def run(*args):
        if args[:2]==('agent','start'):starts.append(args[2]);return {}
        if args[:2]==('agent','prompt'):prompts.append(args[2]);return {}
        raise AssertionError(args)
    monkeypatch.setattr(boot,'run',run)
    monkeypatch.setattr(trust,'ensure_trusted',lambda *a,**kw:None)
    kind='claude' if screen==CLAUDE else 'codex'
    def info(pane):
        slug=pane.split(':')[0]
        return {**env.info,'pane_id':pane,'name':slug+'-exec','agent':kind,'agent_status':'blocked' if slug=='alpha' else 'idle'}
    monkeypatch.setattr(boot.core,'get_agent_info',info)
    monkeypatch.setattr(startup,'read_detection',lambda runtime,pane:screen if pane.startswith('alpha:') else '')
    monkeypatch.setattr(startup,'_send_enter',lambda *a:pytest.fail('bulk bootstrap pressed a key'))
    monkeypatch.setattr(sys,'argv',['herdr-bootstrap'])
    assert boot.main()==1
    assert starts==['alpha-exec','beta-exec'] and prompts==['beta-exec']
    error=capsys.readouterr().err
    assert 'alpha:p1' in error and startup.dialog_excerpt(screen) in error and 'agents parados' in error


@pytest.mark.parametrize('screen',['Working directory\n› Other choice\nUse session directory','Press R to continue'])
def test_rename_never_calls_legacy_dialog_key_handler(launch,monkeypatch,screen):
    env=launch;rename=load('herdr-rename-space');calls=[]
    monkeypatch.setattr(rename.time,'sleep',lambda *a:None)
    monkeypatch.setattr(rename.core,'get_agent_info',lambda target:dict(env.info))
    monkeypatch.setattr(startup,'read_detection',lambda runtime,pane:screen)
    def run(command,**kwargs):
        calls.append(command)
        assert 'send-keys' not in command and 'send-text' not in command and 'prompt' not in command
        return subprocess.CompletedProcess(command,0,'','')
    monkeypatch.setattr(rename.subprocess,'run',run)
    rs=SimpleNamespace(start_args=lambda *a:[],tratar_dialogos=lambda *a:pytest.fail('legacy handler can send unguarded keys'))
    ag={'pane':'w:p2','novo_nome':'sample-rev-1','kind':'codex','sessao':'s','model':'m','effort':'e','argv':[]}
    result=rename.religar(SimpleNamespace(),{'agentes':[ag]},rs,str(env.project))[0]
    assert not result['ok'] and 'w:p2' in result['erro'] and startup.dialog_excerpt(screen) in result['erro']
    assert result['dialogos']==[]


def test_shared_blocked_diagnostic_does_not_retain_terminal_history(launch,monkeypatch):
    env=launch
    monkeypatch.setattr(startup,'read_detection',lambda *a:'PRIVATE PRIOR TASK\n'+('x'*5000)+'\n'+CLAUDE)
    text=startup.blocked_diagnostic(env.boot.startup_runtime(),{**env.info,'agent':'claude'})
    assert 'w:p2' in text and 'confiança Claude' in text and 'No, exit' in text
    assert 'PRIVATE' not in text and 'xxx' not in text and len(text)<600


def test_startup_refusal_cannot_leak_history_through_bootstrap_stderr(launch):
    env=launch;env.kind[0]='claude';env.status[0]='blocked'
    env.screen[0]='PRIVATE PRIOR TASK SENTINEL\n'+('x'*5000)+'\n'+CLAUDE
    with pytest.raises(startup.StartupDialogRefused) as exc:
        startup.finish_start(env.boot.startup_runtime(),None,pane='w:p2',name='sample-rev-1',kind='claude')
    message=str(exc.value)
    assert 'w:p2' in message and 'No, exit' in message
    assert 'SENTINEL' not in message and 'xxx' not in message and len(message)<700


def test_missing_reviewer_recovery_propagates_only_reduced_startup_error(launch,monkeypatch):
    env=launch;core=load('_herdr_dispatch.py');env.screen[0]='PRIVATE PRIOR TASK SENTINEL\n'+('x'*5000)+'\n'+CLAUDE
    env.kind[0]='claude';env.status[0]='blocked'
    with pytest.raises(startup.StartupDialogRefused) as refused:
        startup.finish_start(env.boot.startup_runtime(),None,pane='w:p2',name='sample-rev-1',kind='claude')
    monkeypatch.setattr(core,'get_agent_info',lambda name:(_ for _ in ()).throw(RuntimeError('agent_not_found')))
    monkeypatch.setattr(core.review_groups,'get_group',lambda slug:None)
    monkeypatch.setattr(core.subprocess,'run',lambda cmd,**kw:subprocess.CompletedProcess(cmd,1,'',str(refused.value)))
    with pytest.raises(core.AgentRestoreError) as recovered:
        core._ensure_required_agents_locked('sample',str(env.project),['sample-rev-1'])
    message=str(recovered.value)
    assert 'w:p2' in message and 'No, exit' in message and 'SENTINEL' not in message and 'xxx' not in message
    assert len(message)<800
