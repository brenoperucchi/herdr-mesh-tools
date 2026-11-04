"""Additional repositories: pure preview and temporary HOME configuration only."""
import ast
import copy
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import pytest

BIN=Path(__file__).resolve().parents[1]/'bin'
sys.path.insert(0,str(BIN))
import _herdr_trust as trust


def load(name):
    loader=SourceFileLoader(name.replace('-','_')+'_multi_tests',str(BIN/name))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    mod=importlib.util.module_from_spec(spec);loader.exec_module(mod);return mod


@pytest.fixture
def home(tmp_path,monkeypatch):
    p=tmp_path/'home';p.mkdir()
    monkeypatch.setenv('HOME',str(p));monkeypatch.setenv('CODEX_HOME',str(p/'.codex'))
    monkeypatch.setenv('HERDR_RENAME_HOME',str(p));monkeypatch.setenv('HERDR_MESH_STATE_DIR',str(p/'state'))
    return p


def table(home,extra):
    p=home/'bootstrap'
    p.write_text('SPACES = [("sample", f"{HOME}/base", None, None, "sample", '
                 '{"lean": True, "additional_repositories": '+extra+'})]\n')
    return p


def test_ast_registry_lists_base_and_additional_without_executing(home):
    p=table(home,'[f"{HOME}/one", f"{HOME}/two", f"{HOME}/one"]')
    assert trust.registered_directories(p,home)=={str(home/x) for x in ['base','one','two']}


@pytest.mark.parametrize('extra',['"not a list"','[do_not_execute()]','["relative"]','[HOME]',
                                   '["/"]','[f"{HOME}"]','[f"{HOME}/scratchpad/x"]'])
def test_ast_malformed_or_forbidden_additional_refuses_entire_entry(home,extra):
    p=table(home,extra);refused=[]
    assert trust.registered_directories(p,home,refused)==set()
    assert refused and refused[0]['status']=='refused'


def test_add_space_options_and_additional_roundtrip(home):
    add=load('herdr-add-space')
    dirs=[str(home/'one'),str(home/'two')]
    opts={'custom':{'retain':'value'},'lean':True}
    before=copy.deepcopy(opts)
    text=add.render_entry('sample',str(home/'base'),'sample','claude','codex','',
                          options=opts,additional_repositories=dirs)
    ns={'HOME':str(home),'EXEC_ARGS':[],'SCOUT_ARGS':[]}
    exec('SPACES = [\n'+text+']',ns)
    assert ns['SPACES'][0][5]=={**opts,'additional_repositories':dirs}
    assert opts==before
    p=home/'table';p.write_text('SPACES = [\n'+text+']\n')
    assert trust.registered_directories(p,home)=={str(home/'base'),*dirs}


def test_rename_preserves_options_including_old_slug_strings(home,monkeypatch):
    rename=load('herdr-rename-space')
    p=home/'bootstrap'
    p.write_text('SPACES = [\n    ("old", f"{HOME}/base", ("old-exec", "claude", []),\n'
                 '        None, "old", {"lean": True, "custom": ["old", "old-exec"],\n'
                 '                      "additional_repositories": [f"{HOME}/one", f"{HOME}/two"]}),\n]\n')
    monkeypatch.setattr(rename,'BOOTSTRAP',str(p));monkeypatch.setattr(rename,'HOME',str(home))
    args=SimpleNamespace(de='old',label='new',slug='new')
    result=rename.edita_spaces(args,{'slug_velho':'old'},str(home/'renamed'),False,str(home/'backup'))
    assert '"custom": ["old", "old-exec"]' in result['depois']
    assert 'f"{HOME}/one", f"{HOME}/two"' in result['depois']
    assert '"new-exec"' in result['depois']
    assert p.read_text().find('"new-exec"')==-1


def test_launch_preview_and_catalog_are_pure(home):
    boot=load('herdr-bootstrap')
    base=home/'base';base.mkdir();one=home/'one';one.mkdir();two=home/'two';two.mkdir()
    entry=('sample',str(base),('sample-exec','claude',['--model','pinned']),None,'sample',
           {'lean':True,'additional_repositories':[str(one),str(two)]})
    argv=boot.launch_args(entry,'sample-exec','claude',['--model','pinned'])
    assert argv==['--model','pinned','--add-dir',str(one),str(two)]
    assert boot.launch_args(entry,'sample-rev-2','claude',[],review_repo=str(two))==['--add-dir',str(two)]
    assert boot.launch_args(entry,'sample-scout','claude',[])==[]
    assert boot.launch_args(entry,'sample-exec','codex',['-m','pinned'])==['-m','pinned']
    c=boot.catalog_entry(entry)
    assert c['repositories']==[str(base),str(one),str(two)]
    assert {a['name'] for a in c['agents']} >= {'sample-exec','sample-rev-2'}
    assert not (home/'.claude.json').exists() and not (home/'.codex').exists()


def test_review_repo_must_be_registered_for_launch(home):
    boot=load('herdr-bootstrap');base=home/'base';base.mkdir()
    other=home/'outside';other.mkdir()
    entry=('sample',str(base),('sample-exec','claude',[]),None,'sample',{'lean':True})
    with pytest.raises(RuntimeError):boot.launch_args(entry,'sample-rev-2','claude',[],review_repo=str(other))


def test_catalog_cli_never_contacts_herdr(home,monkeypatch,capsys):
    boot=load('herdr-bootstrap');(home/'base').mkdir();(home/'one').mkdir()
    entry=('sample',str(home/'base'),('sample-exec','claude',[]),None,'sample',
           {'lean':True,'additional_repositories':[str(home/'one')]})
    monkeypatch.setattr(boot,'SPACES',[entry])
    monkeypatch.setattr(boot,'api',lambda *a:(_ for _ in ()).throw(AssertionError('Herdr contacted')))
    monkeypatch.setattr(sys,'argv',['herdr-bootstrap','--catalog','--slug','sample'])
    assert boot.main()==0
    assert json.loads(capsys.readouterr().out)[0]['repositories']==[str(home/'base'),str(home/'one')]


def test_selected_reviewer_recovery_propagates_target(home,monkeypatch):
    import subprocess
    core=load('_herdr_dispatch.py');commands=[]
    def get(name):
        if not commands:raise RuntimeError('agent_not_found')
        return {'agent_status':'idle','pane_id':'w:p1','agent':'claude'}
    monkeypatch.setattr(core,'get_agent_info',get)
    monkeypatch.setattr(core,'_wait_restored_agent',lambda n:get(n))
    def run(command,**kwargs):
        commands.append(command);return subprocess.CompletedProcess(command,0,'','')
    monkeypatch.setattr(core.subprocess,'run',run)
    core.ensure_required_agents('sample',str(home),['sample-rev-2'],selected_only=True,
                                review_repo=str(home/'target'))
    assert commands[0][-2:]==['--review-repo',str(home/'target')]
    assert '--role' in commands[0] and '--with-reviewers' not in commands[0]


def test_bootstrap_new_claude_reviewer_receives_only_selected_repo(home,monkeypatch):
    boot=load('herdr-bootstrap');base=home/'base';base.mkdir()
    one=home/'one';one.mkdir();two=home/'two';two.mkdir()
    entry=('sample',str(base),('sample-exec','claude',['--model','exec-profile']),None,'sample',
           {'lean':True,'additional_repositories':[str(one),str(two)]})
    monkeypatch.setattr(boot,'SPACES',[entry])
    monkeypatch.setattr(boot.core,'project_root',lambda p:str(base))
    monkeypatch.setattr(boot.migration,'space_gate_message',lambda *a:None)
    monkeypatch.setattr(boot,'build_rev_agents',lambda *a:[('sample-rev-1','codex',[]),('sample-rev-2','claude',['--model','review-profile'])])
    monkeypatch.setattr(boot,'find_unnamed_reviewer_base',lambda *a,**k:None)
    monkeypatch.setattr(boot,'ensure_notify_watchers',lambda *a:None)
    monkeypatch.setattr(boot.startup,'created_pane_receipt',lambda *a,**k:None)
    def api(*a):
        if a==('workspace','list'):return {'workspaces':[{'label':'sample','workspace_id':'w'}]}
        if a==('pane','list'):return {'panes':[{'pane_id':'w:p1','tab_id':'w:t1','workspace_id':'w'}]}
        if a==('agent','list'):return {'agents':[{'name':'sample-exec','pane_id':'w:p1','tab_id':'w:t1','workspace_id':'w'}]}
        raise AssertionError(a)
    monkeypatch.setattr(boot,'api',api)
    monkeypatch.setattr(boot,'run',lambda *a:{'pane':{'pane_id':'w:p2'}})
    starts=[]
    monkeypatch.setattr(boot,'start_agent_with_role',lambda pane,name,kind,extra,*a,**k:starts.append((name,kind,extra)))
    monkeypatch.setattr(sys,'argv',['herdr-bootstrap','--slug','sample','--role','rev-2','--review-repo',str(two)])
    assert boot.main()==0
    assert starts==[('sample-rev-2','claude',['--model','review-profile','--add-dir',str(two)])]
    assert entry[2][2]==['--model','exec-profile']


def test_trust_sync_dry_run_preserves_all_fake_configuration(home,monkeypatch,capsys):
    base=home/'base';base.mkdir();one=home/'one';one.mkdir()
    store=trust.TrustStore(home=home,registered=[base,one])
    codex=home/'.codex/config.toml';codex.parent.mkdir()
    codex.write_text('# keep comment\n[projects.'+json.dumps(str(base))+']\ntrust_level="trusted"\n')
    claude=home/'.claude.json';claude.write_text('{"counter":7,"projects":{}}')
    before={p:p.read_bytes() for p in (codex,claude)}
    sync=load('herdr-trust-sync')
    monkeypatch.setattr(sync.trust,'TrustStore',lambda:store)
    monkeypatch.setattr(sync.trust,'ensure_trusted',lambda *a,**k:(_ for _ in ()).throw(AssertionError('write attempted')))
    monkeypatch.setattr(sys,'argv',['herdr-trust-sync'])
    assert sync.main()==0
    data=json.loads(capsys.readouterr().out)
    assert len(data['entries'])==4
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert not (home/'.local/state/herdr-trust').exists()


def test_ast_option_unpack_cannot_smuggle_registration(home):
    p=home/'bootstrap'
    p.write_text('SPACES=[("sample",f"{HOME}/base",None,None,"sample",'
                 '{**untrusted(),"additional_repositories":[f"{HOME}/one"]})]\n')
    refused=[]
    assert trust.registered_directories(p,home,refused)==set() and refused


def test_rename_apply_preserves_custom_options_and_other_spaces(home,monkeypatch):
    rename=load('herdr-rename-space');p=home/'bootstrap'
    original=('SPACES = [\n    ("old", f"{HOME}/base", ("old-exec", "claude", []),\n'
              '        None, "old", {"lean": True, "custom": ["old", "old-exec"],\n'
              '                      "additional_repositories": [f"{HOME}/one"]}),\n'
              '    ("neighbor", f"{HOME}/neighbor", None, None),\n]\n')
    p.write_text(original)
    monkeypatch.setattr(rename,'BOOTSTRAP',str(p));monkeypatch.setattr(rename,'HOME',str(home))
    args=SimpleNamespace(de='old',label='new',slug='new')
    rename.edita_spaces(args,{'slug_velho':'old'},str(home/'renamed'),True,str(home/'backup'))
    ns={'HOME':str(home)};exec(p.read_text(),ns)
    assert ns['SPACES'][0][5]=={'lean':True,'custom':['old','old-exec'],
                               'additional_repositories':[str(home/'one')]}
    assert ns['SPACES'][1]==('neighbor',str(home/'neighbor'),None,None)
    assert list((home/'backup').iterdir())


@pytest.mark.parametrize('kind',['claude','codex'])
def test_conflicting_native_add_dir_is_never_silently_replaced(home,kind):
    boot=load('herdr-bootstrap');base=home/'base';base.mkdir();one=home/'one';one.mkdir()
    entry=('sample',str(base),('sample-exec',kind,[]),None,'sample',
           {'additional_repositories':[str(one)]})
    args=['--add-dir',str(home/'human-choice')]
    if kind=='claude':
        with pytest.raises(RuntimeError):boot.launch_args(entry,'sample-exec',kind,args)
    else:
        assert boot.launch_args(entry,'sample-exec',kind,args)==args


def add_cli(home,monkeypatch):
    import subprocess
    add=load('herdr-add-space');p=home/'table';p.write_text('SPACES = []\n')
    monkeypatch.setattr(add,'BOOTSTRAP',str(p))
    monkeypatch.setattr(add,'categorias_validas',lambda:['test'])
    monkeypatch.setattr(add,'foco_do_guardian',lambda:None)
    monkeypatch.setattr(add,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,'{"result":{"workspaces":[]}}',''))
    original=subprocess.run
    def run(cmd,**kw):
        if cmd[0]=='git':return original(cmd,**kw)
        return subprocess.CompletedProcess(cmd,0,'','')
    monkeypatch.setattr(add.subprocess,'run',run)
    return add,p


@pytest.mark.parametrize('case',['duplicate','base','alias'])
def test_add_cli_normalizes_before_render_and_scope_accepts(home,monkeypatch,case):
    import subprocess
    import _herdr_review_scope as scope
    base=home/'base';base.mkdir();extra=home/'extra';extra.mkdir()
    for p in (base,extra):subprocess.run(['git','init','--quiet',str(p)],check=True)
    alias=home/'alias';alias.symlink_to(extra,target_is_directory=True)
    supplied={'duplicate':[extra,extra],'base':[base,extra], 'alias':[extra,alias]}[case]
    add,p=add_cli(home,monkeypatch);rendered=[];original=add.render_entry
    def render(*a,**kw):
        text=original(*a,**kw);rendered.append(text);return text
    monkeypatch.setattr(add,'render_entry',render)
    argv=['herdr-add-space','sample',str(base),'--dry-run','--categoria','test']
    for item in supplied:argv+=['--additional-repo',str(item)]
    monkeypatch.setattr(sys,'argv',argv)
    assert add.main()==0 and p.read_text()=='SPACES = []\n'
    ns={'HOME':str(home),'EXEC_ARGS':[],'SCOUT_ARGS':[]};exec('ENTRIES=[\n'+rendered[0]+']',ns)
    entry=ns['ENTRIES'][0]
    assert entry[5]['additional_repositories']==[str(extra)]
    monkeypatch.setattr(scope,'entries',lambda:[entry])
    owner=dict(name='sample-exec',agent='claude',cwd=str(base),foreground_cwd=str(base),
               agent_session={'value':'session'},pane_id='w:p1',tab_id='w:t1',workspace_id='w')
    core=SimpleNamespace(get_agent_info=lambda n:owner)
    assert scope.resolve('sample',str(extra),str(base),core)['repositories']==[str(base),str(extra)]


@pytest.mark.parametrize('no_git',[False,True])
def test_add_cli_with_additional_refuses_non_git_base_before_writing(home,monkeypatch,no_git):
    import subprocess
    base=home/'base';base.mkdir();extra=home/'extra';extra.mkdir()
    subprocess.run(['git','init','--quiet',str(extra)],check=True)
    add,p=add_cli(home,monkeypatch)
    argv=['herdr-add-space','sample',str(base),'--dry-run','--categoria','test','--additional-repo',str(extra)]
    if no_git:argv+=['--no-git']
    monkeypatch.setattr(sys,'argv',argv)
    with pytest.raises(SystemExit):add.main()
    assert p.read_text()=='SPACES = []\n'


@pytest.mark.parametrize('bad_alive',[False,True])
def test_global_bootstrap_bad_additional_does_not_block_healthy_space(home,monkeypatch,capsys,bad_alive):
    boot=load('herdr-bootstrap');bad=home/'bad';bad.mkdir();good=home/'good';good.mkdir()
    monkeypatch.setattr(boot,'SPACES',[
        ('bad',str(bad),('bad-exec','claude',[]),None,'bad',{'lean':True,'additional_repositories':[str(home/'missing')]}),
        ('good',str(good),('good-exec','claude',[]),None,'good',{'lean':True})])
    monkeypatch.setattr(boot.core,'project_root',lambda p:str(Path(p).resolve()))
    monkeypatch.setattr(boot.migration,'space_gate_message',lambda *a:None)
    monkeypatch.setattr(boot,'find_unnamed_reviewer_base',lambda *a,**k:None)
    monkeypatch.setattr(boot,'ensure_notify_watchers',lambda *a:None)
    agents=[dict(name='bad-exec',pane_id='w1:p1',tab_id='w1:t1',workspace_id='w1')] if bad_alive else []
    def api(*a):
        if a==('workspace','list'):return {'workspaces':[dict(label='bad',workspace_id='w1'),dict(label='good',workspace_id='w2')]}
        if a==('pane','list'):return {'panes':[dict(pane_id='w1:p1',tab_id='w1:t1',workspace_id='w1'),dict(pane_id='w2:p1',tab_id='w2:t1',workspace_id='w2')]}
        if a==('agent','list'):return {'agents':agents}
        raise AssertionError(a)
    monkeypatch.setattr(boot,'api',api)
    monkeypatch.setattr(boot,'run',lambda *a:(_ for _ in ()).throw(AssertionError('unexpected mutation '+str(a))))
    starts=[];monkeypatch.setattr(boot,'start_agent_with_role',lambda pane,name,*a,**k:starts.append(name))
    monkeypatch.setattr(sys,'argv',['herdr-bootstrap'])
    assert boot.main()==(0 if bad_alive else 1)
    assert starts==['good-exec']
    out=capsys.readouterr()
    assert 'good' in out.out
    if not bad_alive:assert 'bad-exec' in out.err


def test_catalog_keeps_healthy_entries_when_one_configuration_is_invalid(home,monkeypatch,capsys):
    boot=load('herdr-bootstrap');(home/'good').mkdir()
    monkeypatch.setattr(boot,'SPACES',[
        ('bad',str(home/'bad'),None,None,'bad',{'additional_repositories':'invalid'}),
        ('good',str(home/'good'),None,None,'good',{'lean':True})])
    monkeypatch.setattr(sys,'argv',['herdr-bootstrap','--catalog'])
    assert boot.main()==1
    data=json.loads(capsys.readouterr().out)
    assert data[0]['label']=='bad' and data[0]['error']
    assert data[1]['label']=='good' and not data[1].get('error')
