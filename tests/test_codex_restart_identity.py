"""Session selection and duplicate resume checks never operate real panes/configs."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest import mock
import pytest

BIN=Path(__file__).resolve().parents[1]/'bin';sys.path.insert(0,str(BIN))
A='11111111-1111-4111-8111-111111111111'
B='22222222-2222-4222-8222-222222222222'
C='33333333-3333-4333-8333-333333333333'


def load():
    loader=SourceFileLoader('restart_identity_tests',str(BIN/'herdr-agent-restart'))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    m=importlib.util.module_from_spec(spec);loader.exec_module(m);return m


def proc(pid,parent,argv=None,name='codex'):
    return dict(pid=pid,ppid=parent,start=str(pid),argv=argv or [],name=name,state='S')


@pytest.fixture
def world(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir();root=home/'.codex';root.mkdir()
    monkeypatch.setenv('HOME',str(home));monkeypatch.setenv('CODEX_HOME',str(root))
    monkeypatch.setenv('HERDR_MESH_STATE_DIR',str(home/'mesh-state'))
    monkeypatch.delenv('HERDR_PANE_ID',raising=False)
    monkeypatch.delenv('HERDR_MESH_CACHE_DIR',raising=False)
    ca=home/'a';ca.mkdir();cb=home/'b';cb.mkdir()
    path=root/'state_5.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,updated_at_ms INTEGER)')
        db.executemany('INSERT INTO threads VALUES(?,?,?)',[(A,str(ca),200),(B,str(cb),900),(C,str(ca),100)])
    info=dict(name='a-exec',pane_id='w1:p1',agent='codex',agent_status='idle',
              cwd=str(ca),foreground_cwd=str(ca),agent_session={'value':A},terminal_id='term-a',workspace_id='w1',tab_id='w1:t1')
    processes={100:proc(100,1,name='bash'),101:proc(101,100,['codex','resume',A]),
               200:proc(200,1,name='bash')}
    processes[101]['cwd']=str(ca)
    pane=dict(pane_id='w1:p1',shell_pid=100,foreground_processes=[{'pid':101}])
    m=load();calls=[];live=[info];current_pane=dict(info);gets=[]
    # O handshake pos-restart tem testes proprios (test_codex_handshake_cli); aqui a propriedade e' 'sem agent get apos o kill'.
    # A guarda de quem roda tem testes proprios (test_restart_guard); aqui o foco e' a identidade fisica.
    monkeypatch.setattr(m.restart_guard,'authorize',lambda *a,**k:None)
    monkeypatch.setattr(m.review_wake,'proven_caller_context',lambda *a,**k:__import__('contextlib').nullcontext('caller-pane'))
    handshake_calls=[]
    monkeypatch.setattr(m.codex_handshake,'after_restart',lambda *a,**k:handshake_calls.append((a[:3],k)) or ('stubbed',None))
    def stopped(pid):
        processes.pop(pid,None);live.clear()
        current_pane.update(agent=None,agent_session=None,agent_status='unknown')
        pane['foreground_processes']=[{'pid':100,'cwd':str(ca)}]
        return True
    stop=mock.Mock(side_effect=stopped)
    monkeypatch.setattr(m,'agentes',lambda:list(live))
    monkeypatch.setattr(m,'processos',lambda:{'w1:p1':(101,'codex',['codex','resume',A,'-m','gpt-6.1-sol','-c','model_reasoning_effort=low'])})
    monkeypatch.setattr(m.probe_cache,'ler',lambda:{})
    def api(*a):
        if a[:2]==('pane','process-info'):return {'process_info':pane}
        if a[:2]==('pane','get'):return {'pane':current_pane}
        raise AssertionError('unexpected API: '+repr(a))
    def get(n):
        gets.append(n)
        if 101 not in processes:raise RuntimeError('agent_not_found')
        return info
    monkeypatch.setattr(m.core,'api',api)
    monkeypatch.setattr(m.core,'get_agent_info',get)
    monkeypatch.setattr(m,'encerrar',stop);monkeypatch.setattr(m,'prender',lambda pid:pid);monkeypatch.setattr(m,'encerrar_preso',lambda h,*a,**k:stop(h));monkeypatch.setattr(m,'soltar',lambda h:None)
    monkeypatch.setattr(m,'checar_seguranca',lambda info:[])
    monkeypatch.setattr(m,'tratar_dialogos',lambda *a:[])
    monkeypatch.setattr(m,'log_restart',lambda *a:None)
    monkeypatch.setattr(m.time,'sleep',lambda *a:None)
    def run(cmd,**kw):
        calls.append(cmd);return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(m.subprocess,'run',run)
    native_proc_reader=m.restart_identity.process_table if hasattr(m,'restart_identity') else None
    if hasattr(m,'restart_identity'):
        monkeypatch.setattr(m.restart_identity,'process_table',lambda:processes)
    def invoke(*args):
        monkeypatch.setattr(sys,'argv',['herdr-agent-restart','a-exec',*args])
        try:return m.main(),''
        except SystemExit as e:return e.code if isinstance(e.code,int) else 1,str(e)
    return SimpleNamespace(handshake_calls=handshake_calls,m=m,home=home,root=root,db=path,info=info,processes=processes,pane=pane,
                           ca=ca,cb=cb,stop=stop,calls=calls,invoke=invoke,native_proc_reader=native_proc_reader,
                           live=live,current_pane=current_pane,gets=gets,stopped=stopped)


@pytest.mark.parametrize('force',[False,True])
def test_two_agents_pointing_to_same_foreign_session_refuse_before_sigterm(world,force,capsys):
    w=world;w.info['agent_session']={'value':B}
    other={**w.info,'name':'b-scout','pane_id':'w1:p2','cwd':str(w.cb),'foreground_cwd':str(w.cb)}
    w.m.agentes=lambda:[w.info,other]
    code,error=w.invoke(*(['--force'] if force else []))
    output=capsys.readouterr().out+error
    assert code==1 and all(x in output for x in ['w1:p1',B,str(w.ca),str(w.cb)])
    w.stop.assert_not_called();assert w.calls==[]


def test_duplicate_explicit_resume_outside_target_pane_is_refused_with_pid(world,capsys):
    w=world;w.processes[201]=proc(201,200,['codex','resume',A])
    code,error=w.invoke()
    assert code==1 and '201' in capsys.readouterr().out+error
    w.stop.assert_not_called();assert not w.calls


def test_missing_registry_session_never_falls_back_to_process_argv_for_apply(world):
    w=world;w.info['agent_session']=None
    code,error=w.invoke()
    assert code==1 and '--session' in error
    w.stop.assert_not_called();assert not w.calls


def test_missing_registry_session_only_suggests_latest_exact_cwd_in_dry_run(world,capsys):
    w=world;w.info['agent_session']=None
    code,error=w.invoke('--dry-run')
    output=capsys.readouterr().out+error
    assert code==0 and A in output and B not in output and '--session' in output
    w.stop.assert_not_called();assert not w.calls


def test_explicit_session_with_valid_cwd_is_preview_only(world,capsys):
    w=world;w.info['agent_session']=None
    code,error=w.invoke('--dry-run','--session',A)
    assert code==0 and A in capsys.readouterr().out
    w.stop.assert_not_called();assert not w.calls


def test_target_pid_must_be_in_verified_target_pane(world,capsys):
    w=world;w.processes[101]['ppid']=200
    code,error=w.invoke()
    assert code==1
    w.stop.assert_not_called();assert not w.calls


def test_valid_registered_session_is_rechecked_before_actual_start(world):
    w=world
    code,error=w.invoke()
    assert code==0
    w.stop.assert_called_once_with(101)
    assert len(w.calls)==1 and A in w.calls[0]


def test_session_index_failure_never_stops_process(world):
    w=world;w.db.unlink()
    code,error=w.invoke('--force')
    assert code==1
    w.stop.assert_not_called();assert not w.calls


def test_same_repo_different_subdirectory_is_not_equivalent(world):
    w=world
    with sqlite3.connect(w.db) as db:db.execute('UPDATE threads SET cwd=? WHERE id=?',(str(w.ca/'nested'),A))
    assert w.invoke()[0]==1
    w.stop.assert_not_called()


def test_missing_thread_row_reports_absence_not_contextmanager_failure(world):
    w=world
    with sqlite3.connect(w.db) as db:db.execute('DELETE FROM threads WHERE id=?',(A,))
    code,error=w.invoke()
    assert code==1 and 'ausente' in error
    w.stop.assert_not_called()


def test_suggestion_with_unknown_timestamp_schema_refuses_cleanly(world):
    w=world;w.info['agent_session']=None
    with sqlite3.connect(w.db) as db:db.execute('ALTER TABLE threads DROP COLUMN updated_at_ms')
    code,error=w.invoke('--dry-run')
    assert code==1 and 'updated_at_ms' in error
    w.stop.assert_not_called()


def test_committed_wal_is_read_without_mutating_original_index_or_sidecars(world):
    w=world;identity=w.m.restart_identity
    db=sqlite3.connect(w.db)
    try:
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('UPDATE threads SET updated_at_ms=? WHERE id=?',(2000,C));db.commit()
        before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in w.root.iterdir()}
        assert identity.latest(w.info)['session']==C
        assert identity.thread(C)==str(w.ca)
        after={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in w.root.iterdir()}
        assert after==before
    finally:db.close()


def test_orphan_wal_read_never_creates_source_shm(world,monkeypatch):
    import shutil
    w=world;identity=w.m.restart_identity
    db=sqlite3.connect(w.db)
    try:
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('UPDATE threads SET updated_at_ms=? WHERE id=?',(3000,C));db.commit()
        other=w.home/'orphan-index';other.mkdir()
        for suffix in ('','-wal'):
            shutil.copyfile(str(w.db)+suffix,str(other/'state_5.sqlite')+suffix)
        monkeypatch.setenv('CODEX_HOME',str(other))
        before={p.name:p.read_bytes() for p in other.iterdir()}
        assert identity.latest(w.info)['session']==C
        assert {p.name:p.read_bytes() for p in other.iterdir()}==before
        assert not (other/'state_5.sqlite-shm').exists()
    finally:db.close()


@pytest.mark.parametrize('argv,expected',[
    (['codex','-p','resume','resume',A],A),
    (['codex','--profile=resume','resume',A],A),
    (['codex','-c','key="resume"','app-server'],None),
    (['codex','exec','echo',A],None),
    (['codex','review','resume'],None),
    (['codex','resume','--model','gpt-6.1-sol',A],A),
])
def test_resume_parser_does_not_confuse_option_values_or_noninteractive_commands(world,argv,expected):
    assert world.m.restart_identity.resume_id(argv)==expected


@pytest.mark.parametrize('argv',[
    ['codex','--image=/fake/one.png','/fake/two.png','-m','gpt-6.1-sol','resume',C],   # posicional antes do subcomando
    ['codex','um prompt','resume',C],
    ['codex','resume','--last'],                                                        # resume sem id explicito
    ['codex','resume'],
    ['codex','resume','--','x'],
    ['codex','exec','resume',A],                                                      # exec resume tambem retoma
    ['codex','e','resume',A,'faz x'],
    ['codex','exec','--json','resume',A],
    ['codex','exec','resume','--last'],
])
def test_resume_parser_refuses_every_form_it_cannot_close(world,argv):
    ri=world.m.restart_identity
    with pytest.raises(ri.Refused):ri.resume_id(argv)


@pytest.mark.parametrize('flags',[(),('--force',)])
def test_positional_before_resume_cannot_hide_a_contradicting_session(world,flags):
    w=world
    argv=['codex','--image=/fake/one.png','/fake/two.png','-m','gpt-6.1-sol','-c','model_reasoning_effort=low','resume',C]
    w.processes[101]['argv']=argv
    w.m.processos=lambda:{'w1:p1':(101,'codex',argv)}
    code,error=w.invoke(*flags)
    assert code==1
    w.stop.assert_not_called();assert not w.calls


def test_neighbor_with_positional_before_resume_is_not_dropped_from_the_filter(world):
    w=world;w.info['agent_session']=None
    w.processes[300]=proc(300,1,['codex','--image=/fake/one.png','/fake/two.png','resume',C]);w.processes[300]['cwd']=str(w.ca)
    code,error=w.invoke('--dry-run')
    assert code==1 and 'argv' in error        # argv ambiguo de vizinho: recusa a sugestao, nunca a ignora


def test_foreign_pid_claiming_target_environment_is_still_refused(world,monkeypatch,capsys):
    w=world;identity=w.m.restart_identity;root=w.home/'proc';root.mkdir()
    for pid,parent,argv,name in [(100,1,[],'bash'),(101,100,['codex','resume',A],'codex'),
                                  (200,1,[],'bash'),(201,200,['codex','resume',A],'codex')]:
        d=root/str(pid);d.mkdir()
        fields=['S',str(parent),*(['0']*17),str(pid)]
        (d/'stat').write_text(f'{pid} ({name}) '+' '.join(fields))
        (d/'cmdline').write_bytes(b'\0'.join(v.encode() for v in argv)+b'\0')
        (d/'environ').write_bytes(b'HERDR_PANE_ID=w1:p1\0')
        (d/'cwd').symlink_to(w.ca if pid in (100,101) else w.cb,target_is_directory=True)
    monkeypatch.setattr(identity,'PROC_ROOT',root)
    monkeypatch.setattr(identity,'process_table',w.native_proc_reader)
    code,error=w.invoke()
    assert code==1 and '201' in capsys.readouterr().out+error
    w.stop.assert_not_called();assert not w.calls


def test_target_pid_start_change_between_checks_never_stops_reused_pid(world,monkeypatch,capsys):
    w=world;original=w.m.validar_codex;count=[0]
    def changed(*a):
        count[0]+=1
        if count[0]==2:w.processes[101]['start']='replacement'
        return original(*a)
    monkeypatch.setattr(w.m,'validar_codex',changed)
    assert w.invoke()[0]==1
    assert 'processo alvo mudou' in capsys.readouterr().out
    w.stop.assert_not_called();assert not w.calls


def test_duplicate_appearing_after_stop_blocks_religamento(world,monkeypatch,capsys):
    w=world
    def stopped(pid):
        w.stopped(pid);w.processes[201]=proc(201,200,['codex','resume',A]);return True
    stop=mock.Mock(side_effect=stopped);monkeypatch.setattr(w.m,'encerrar',stop);monkeypatch.setattr(w.m,'prender',lambda pid:pid);monkeypatch.setattr(w.m,'encerrar_preso',lambda h,*a,**k:stop(h));monkeypatch.setattr(w.m,'soltar',lambda h:None)
    assert w.invoke()[0]==1
    assert '201' in capsys.readouterr().out
    stop.assert_called_once_with(101);assert not w.calls


def test_delayed_self_restart_preserves_verified_explicit_session(world,monkeypatch):
    w=world;spawn=mock.Mock()
    # o proprio pane vem da PROVA nativa (nao da variavel de ambiente); o pai espera o READY do helper
    monkeypatch.setattr(w.m.review_wake,'proven_caller_context',lambda *a,**k:__import__('contextlib').nullcontext('w1:p1'))
    monkeypatch.setattr(w.m.restart_guard,'wait_ready',lambda *a,**k:(True,'READY'))
    monkeypatch.setattr(w.m.subprocess,'Popen',spawn)
    assert w.invoke('--session',A)[0]==0
    command=spawn.call_args.args[0]
    assert '--_separado' in command and command[command.index('--session')+1]==A
    w.stop.assert_not_called();assert not w.calls


def test_native_index_alias_is_compared_by_realpath(world):
    w=world;alias=w.home/'alias';alias.symlink_to(w.ca,target_is_directory=True)
    with sqlite3.connect(w.db) as db:db.execute('UPDATE threads SET cwd=? WHERE id=?',(str(alias),A))
    assert w.invoke('--dry-run')[0]==0
    w.stop.assert_not_called();assert not w.calls


@pytest.mark.parametrize('case',['native_cwd','native_kind'])
def test_target_process_must_match_codex_and_exact_pane_cwd(world,case):
    w=world
    if case=='native_cwd':w.processes[101]['cwd']=str(w.cb)
    else:w.processes[101].update(name='claude',argv=['claude'])
    assert w.invoke('--force')[0]==1
    w.stop.assert_not_called();assert not w.calls


def test_dry_run_missing_session_can_suggest_even_when_pid_is_unobservable(world,monkeypatch,capsys):
    w=world;w.info['agent_session']=None
    monkeypatch.setattr(w.m,'processos',lambda:{})
    code,error=w.invoke('--dry-run')
    assert code==0 and A in capsys.readouterr().out+error
    w.stop.assert_not_called();assert not w.calls


def test_changed_registered_session_in_same_cwd_is_refused_before_stop(world,monkeypatch):
    w=world
    monkeypatch.setattr(w.m.core,'get_agent_info',lambda n:{**w.info,'agent_session':{'value':C}})
    assert w.invoke()[0]==1
    w.stop.assert_not_called();assert not w.calls


@pytest.mark.parametrize('force',[False,True])
def test_duplicate_registered_session_same_cwd_refuses_without_explicit_resume_argv(world,force):
    w=world
    w.live.append({**w.info,'name':'other-scout','pane_id':'w1:p2'})
    code,error=w.invoke(*(['--force'] if force else []))
    assert code==1
    assert 'other-scout' in error and 'w1:p2' in error
    w.stop.assert_not_called();assert not w.calls


def test_no_agent_get_after_kill_and_cleared_registry_still_starts(world):
    w=world
    assert w.invoke()[0]==0
    assert w.gets==['w1:p1','w1:p1']
    assert len(w.calls)==1 and A in w.calls[0]
    # fiacao real do bloco novo (r192/rev-2): nome, pane e terminal_id do info ANTERIOR ao restart
    assert w.handshake_calls and w.handshake_calls[0][0]==('a-exec','w1:p1','term-a')
    assert w.handshake_calls[0][1]['expected_cwd']==w.info['cwd']


@pytest.mark.parametrize('change',['terminal_id','workspace_id','tab_id','pane_id','cwd',
                                   'foreground_cwd','shell_pid','shell_start','foreground','new_agent','process_pane','shell_cwd'])
def test_post_stop_requires_original_pane_shell_and_cwd(world,change,capsys):
    w=world
    def stop(pid):
        w.stopped(pid)
        if change in ('terminal_id','workspace_id','tab_id','pane_id'):
            w.current_pane[change]='replacement'
        elif change in ('cwd','foreground_cwd'):w.current_pane[change]=str(w.cb)
        elif change=='shell_pid':w.pane['shell_pid']=200
        elif change=='shell_start':w.processes[100]['start']='replacement'
        elif change=='foreground':w.pane['foreground_processes']=[{'pid':200}]
        elif change=='process_pane':w.pane['pane_id']='w1:other'
        elif change=='shell_cwd':w.pane['foreground_processes'][0]['cwd']=str(w.cb)
        else:w.current_pane['agent']='claude'
        return True
    w.stop.side_effect=stop
    code,error=w.invoke('--force')
    output=capsys.readouterr().out+error
    assert code==1 and not w.calls
    assert 'Recuperação manual' in output and 'agent start' in output and A in output


def test_duplicate_registry_session_appearing_after_stop_refuses_with_recovery(world,capsys):
    w=world
    def stop(pid):
        w.stopped(pid);w.live.append({**w.info,'name':'other-exec','pane_id':'w1:p2'})
        return True
    w.stop.side_effect=stop
    assert w.invoke()[0]==1
    text=capsys.readouterr().out
    assert 'other-exec' in text and 'Recuperação manual' in text and not w.calls


@pytest.mark.parametrize('failure',['nonzero','timeout','oserror','json_timeout','transport_failed'])
def test_failed_start_prints_exact_recovery_command_and_returns_nonzero(world,monkeypatch,capsys,failure):
    import shlex
    import subprocess
    w=world
    def run(cmd,**kw):
        w.calls.append(cmd)
        if failure=='timeout':raise subprocess.TimeoutExpired(cmd,150)
        if failure=='oserror':raise OSError('test executable unavailable')
        err={'json_timeout':'{"error":{"code":"timeout"}}','transport_failed':'{"error":{"code":"agent_start_transport_failed"}}'}.get(failure,'start refused')
        return SimpleNamespace(returncode=1,stdout='',stderr=err)
    monkeypatch.setattr(w.m.subprocess,'run',run)
    code,error=w.invoke()
    text=capsys.readouterr().out
    assert code==1
    line=next(x for x in text.splitlines() if x.startswith('Recuperação manual: '))
    assert shlex.split(line.removeprefix('Recuperação manual: '))==w.calls[0]
    # r59/rev-1 P1: resultado desconhecido NUNCA repete o start de um Codex (exatamente uma chamada)
    assert len(w.calls)==1



def test_existing_orphan_plan_observes_physical_binding_without_agent_get(world,monkeypatch):
    w=world;w.stopped(101)
    w.info['_orfao']=True
    for field in ('terminal_id','workspace_id','tab_id'):w.info.pop(field,None)
    monkeypatch.setattr(w.m,'achar_alvo',lambda *args:w.info)
    monkeypatch.setattr(w.m,'processos',lambda:{})
    code,error=w.invoke('--session',A)
    assert code==0 and not w.gets and len(w.calls)==1
    w.stop.assert_not_called()


def test_post_stop_failed_registry_read_still_prints_recovery(world,monkeypatch,capsys):
    w=world
    def agents():
        if 101 not in w.processes:raise ValueError('invalid registry JSON')
        return list(w.live)
    monkeypatch.setattr(w.m,'agentes',agents)
    assert w.invoke()[0]==1
    assert 'Recuperação manual' in capsys.readouterr().out and not w.calls


# --- achados r198 (G1) ------------------------------------------------------------------------
def test_live_process_resume_contradicting_the_registered_session_is_refused_before_stop(world):
    w=world
    w.processes[101]['argv']=['codex','resume',C]          # registro diz A; o processo vivo retoma C (mesmo cwd)
    w.m.processos=lambda:{'w1:p1':(101,'codex',['codex','resume',C])}
    for flags in ((),('--force',)):
        code,error=w.invoke(*flags)
        assert code==1 and C in error and A in error
    w.stop.assert_not_called();assert not w.calls


def test_consistent_registered_and_live_resume_still_restarts(world):
    w=world
    assert w.invoke()[0]==0
    w.stop.assert_called_once();assert len(w.calls)==1


def test_explicit_session_different_from_the_registered_one_is_refused(world):
    w=world
    code,error=w.invoke('--session',C)
    assert code==1 and 'diverge da sessão registrada' in error
    w.stop.assert_not_called();assert not w.calls


def test_signal_goes_through_the_pidfd_acquired_before_the_final_validation(world,monkeypatch):
    # r198/rev-1 P1: o SIGTERM nunca vai pelo numero do PID (reuso de PID depois da ultima conferencia)
    w=world;order=[]
    monkeypatch.setattr(w.m,'prender',lambda pid:order.append(('prender',pid)) or 'FD')
    real=w.m.validar_codex
    monkeypatch.setattr(w.m,'validar_codex',lambda *a,**k:order.append(('validar',)) or real(*a,**k))
    monkeypatch.setattr(w.m,'encerrar_preso',lambda h,*a,**k:order.append(('sinal',h)) or w.stop(101))
    monkeypatch.setattr(w.m,'soltar',lambda h:order.append(('soltar',h)))
    monkeypatch.setattr(w.m,'encerrar',lambda *a,**k:pytest.fail('os.kill por numero nao pode ser usado no Codex'))
    assert w.invoke()[0]==0
    kinds=[o[0] for o in order]
    assert kinds.index('prender')<len(kinds)-1-kinds[::-1].index('validar')<kinds.index('sinal')
    assert ('sinal','FD') in order and ('soltar','FD') in order


def test_pidfd_helpers_signal_by_descriptor_and_wait_on_it(monkeypatch):
    m=load();sent=[]
    monkeypatch.setattr(m.signal,'pidfd_send_signal',lambda fd,sig:sent.append((fd,sig)))
    class Poll:
        def register(self,fd,ev):self.fd=fd
        def poll(self,ms):return [(self.fd,1)]
    monkeypatch.setattr(m.select,'poll',lambda:Poll())
    monkeypatch.setattr(m.os,'kill',lambda *a:pytest.fail('kill por numero'))
    assert m.encerrar_preso(7,3) is True and sent==[(7,m.signal.SIGTERM)]
    class Slow(Poll):
        def poll(self,ms):return []
    monkeypatch.setattr(m.select,'poll',lambda:Slow())
    assert m.encerrar_preso(7,1) is False


def test_pid_that_cannot_be_pinned_is_refused_without_signal(world,monkeypatch,capsys):
    w=world
    def gone(pid):raise ProcessLookupError(pid)
    monkeypatch.setattr(w.m,'prender',gone)
    code,error=w.invoke()
    assert code==1 and not w.calls
    w.stop.assert_not_called()


def test_sigterm_without_exit_prints_the_recovery_command(world,monkeypatch,capsys):
    w=world;w.stop.side_effect=None;w.stop.return_value=False
    code,error=w.invoke()
    assert code==1 and 'Recuperação manual' in capsys.readouterr().out and not w.calls


def test_unexpected_exception_after_the_stop_still_prints_the_recovery_command(world,monkeypatch,capsys):
    w=world
    def boom():raise KeyError('result')
    original=w.m.agentes
    def agentes():
        return boom() if 101 not in w.processes else original()
    monkeypatch.setattr(w.m,'agentes',agentes)
    code,error=w.invoke()
    assert code==1 and 'Recuperação manual' in capsys.readouterr().out and not w.calls


def test_dry_run_never_suggests_a_session_declared_by_another_agent(world,capsys):
    w=world;w.info['agent_session']=None
    other=dict(name='a-rev-1',pane_id='w1:p9',agent='codex',agent_session={'value':A})
    w.m.agentes=lambda:[w.info,other]
    w.m.processos=lambda:{}
    code,error=w.invoke('--dry-run')
    output=capsys.readouterr().out+error
    assert code==0 and 'sessão='+A not in output and 'sessão='+C in output     # a vizinha e' excluida; sobra a seguinte


def test_dry_run_refuses_to_suggest_when_another_live_codex_in_the_cwd_has_no_provable_session(world,capsys):
    w=world;w.info['agent_session']=None
    w.processes[300]=proc(300,1,['codex','-m','gpt-6.1-sol'])      # vizinho sem `resume` no argv
    w.processes[300]['cwd']=str(w.ca)
    code,error=w.invoke('--dry-run')
    assert code==1 and 'sem sessão comprovável' in error
    w.stop.assert_not_called();assert not w.calls


# --- achados r199 (G1, rodada 2) ---------------------------------------------------------------
IMG=['codex','--image','/fake/image.png','-m','gpt-6.1-sol','-c','model_reasoning_effort=low','resume',C]


def test_resume_id_understands_variadic_image_and_refuses_the_ambiguous_form():
    ri=load().restart_identity
    assert ri.resume_id(IMG)==C
    assert ri.resume_id(['codex','-i','a.png','b.png','-m','x'])is None
    assert ri.resume_id(['codex','--image=a.png','resume',A])==A
    with pytest.raises(ri.Refused):ri.resume_id(['codex','--image','a.png','resume',A])  # `resume` dentro da lista variadica


@pytest.mark.parametrize('flags',[(),('--force',)])
def test_contradicting_resume_behind_image_option_is_refused_before_stop(world,flags):
    w=world
    w.processes[101]['argv']=IMG
    w.m.processos=lambda:{'w1:p1':(101,'codex',IMG)}
    code,error=w.invoke(*flags)
    assert code==1
    w.stop.assert_not_called();assert not w.calls


def test_unparseable_argv_of_the_target_process_is_refused_not_ignored(world):
    w=world
    bad=['codex','--opcao-que-nao-existe','resume',C]
    w.processes[101]['argv']=bad
    w.m.processos=lambda:{'w1:p1':(101,'codex',bad)}
    code,error=w.invoke('--force')
    assert code==1
    w.stop.assert_not_called();assert not w.calls


def test_dry_run_refuses_when_a_neighbor_has_image_and_no_resume(world):
    w=world;w.info['agent_session']=None
    w.processes[300]=proc(300,1,['codex','--image','/fake/image.png','-m','gpt-6.1-sol']);w.processes[300]['cwd']=str(w.ca)
    code,error=w.invoke('--dry-run')
    assert code==1 and 'sem sessão comprovável' in error


def test_dry_run_refuses_when_a_neighbors_argv_cannot_be_interpreted(world):
    w=world;w.info['agent_session']=None
    w.processes[300]=proc(300,1,['codex','--opcao-que-nao-existe']);w.processes[300]['cwd']=str(w.ca)
    code,error=w.invoke('--dry-run')
    assert code==1 and 'não interpretável' in error


def test_environment_hint_never_turns_an_external_process_into_own(world,capsys):
    # PID 201 e' filho de um shell externo (200) mas processos() o atribui ao pane pela pista de ambiente.
    w=world;w.info['agent_session']=None
    w.processes[200]=proc(200,1,name='bash')
    w.processes[201]=proc(201,200,['codex','resume',A]);w.processes[201]['cwd']=str(w.ca)
    w.processes.pop(101)                     # o alvo nao tem processo proprio; so o externo carrega A
    w.m.processos=lambda:{'w1:p1':(201,'codex',['codex','resume',A])}
    w.pane['foreground_processes']=[{'pid':100}]
    code,error=w.invoke('--dry-run')
    output=capsys.readouterr().out+error
    assert 'sessão='+A not in output          # A pertence ao processo externo: excluida ou sugestao recusada


def test_concurrent_exec_resume_of_the_chosen_session_outside_the_pane_blocks_the_restart(world):
    w=world
    w.processes[300]=proc(300,1,['codex','exec','resume',A]);w.processes[300]['cwd']=str(w.ca)
    code,error=w.invoke()
    assert code==1
    w.stop.assert_not_called();assert not w.calls


# --- achado r202 (G1, rodada 5): resume sem id (--last) fora do pane no mesmo cwd -----------------
@pytest.mark.parametrize('argv',[['codex','resume','--last'],['codex','exec','resume','--last']])
def test_last_form_outside_the_pane_in_the_same_cwd_blocks_before_stop(world,argv):
    w=world
    w.processes[300]=proc(300,1,argv);w.processes[300]['cwd']=str(w.ca)
    code,error=w.invoke()
    assert code==1 and 'mesmo cwd' in error
    w.stop.assert_not_called();assert not w.calls


def test_last_form_in_another_cwd_does_not_block(world):
    w=world
    w.processes[300]=proc(300,1,['codex','resume','--last']);w.processes[300]['cwd']=str(w.cb)
    assert w.invoke()[0]==0


def test_last_form_appearing_after_stop_blocks_religamento_with_recovery(world,monkeypatch,capsys):
    w=world
    def stopped(pid):
        w.stopped(pid);p=proc(201,200,['codex','resume','--last']);p['cwd']=str(w.ca);w.processes[201]=p;return True
    stop=mock.Mock(side_effect=stopped)
    monkeypatch.setattr(w.m,'encerrar',stop);monkeypatch.setattr(w.m,'encerrar_preso',lambda h,*a,**k:stop(h))
    assert w.invoke()[0]==1
    out=capsys.readouterr().out
    assert '201' in out and 'Recuperação manual' in out and not w.calls


# --- cópia do índice fora do /tmp (pedido do coordenador d9d640a0; /tmp é tmpfs/RAM) -------------------------------

def _copies(home):
    base=home/'.cache'/'herdr-mesh-tools'/'tmp'
    return base,(sorted(p.name for p in base.iterdir()) if base.exists() else [])


def test_index_copy_lives_under_the_cache_dir_never_in_tmp_and_is_removed_after_use(world,monkeypatch,tmp_path):
    ident=world.m.restart_identity
    seen=[];kwargs=[]
    real=ident.tempfile.TemporaryDirectory
    def spy(*a,**k):
        d=real(*a,**k);seen.append(d.name);kwargs.append(k);return d
    monkeypatch.setattr(ident.tempfile,'TemporaryDirectory',spy)
    home=Path(__import__('os').environ['HOME'])
    with ident.thread_index() as (db,cols):
        assert db.execute('SELECT count(*) FROM threads').fetchone()[0]==3
        base,names=_copies(home)
        assert len(names)==1 and names[0].startswith('herdr-restart-index-')
    assert seen and all(s.startswith(str(base)+'/') for s in seen)
    assert kwargs and all(k.get('dir')==base for k in kwargs)       # o diretorio e' passado explicitamente (nunca o padrao /tmp)
    assert _copies(home)[1]==[]
    assert oct(base.stat().st_mode & 0o777)=='0o700'


def test_index_copy_is_removed_when_the_consumer_fails_or_the_copy_fails(world,monkeypatch):
    ident=world.m.restart_identity;home=Path(__import__('os').environ['HOME'])
    with pytest.raises(RuntimeError):
        with ident.thread_index():
            raise RuntimeError('consumidor')
    assert _copies(home)[1]==[]
    with pytest.raises(ident.Refused):
        with ident.thread_index():
            raise sqlite3.OperationalError('consulta')
    assert _copies(home)[1]==[]
    # falha inesperada DURANTE a copia (nao OSError/sqlite/Refused): a copia parcial tambem some
    monkeypatch.setattr(ident.sqlite3,'connect',mock.Mock(side_effect=RuntimeError('inesperado')))
    with pytest.raises(RuntimeError):
        with ident.thread_index():
            pass
    assert _copies(home)[1]==[]


def test_size_ceiling_is_kept_and_leaves_no_copy(world,monkeypatch):
    ident=world.m.restart_identity;home=Path(__import__('os').environ['HOME'])
    monkeypatch.setattr(ident,'MAX_COPY_BYTES',10)
    with pytest.raises(ident.Refused):
        with ident.thread_index():
            pass
    assert _copies(home)[1]==[]


def test_stale_copies_from_dead_calls_are_swept_but_fresh_or_foreign_names_are_kept(world):
    ident=world.m.restart_identity;home=Path(__import__('os').environ['HOME'])
    base=ident.copy_base()
    old=base/'herdr-restart-index-dead';old.mkdir();(old/'state_5.sqlite').write_bytes(b'x')
    fresh=base/'herdr-restart-index-live';fresh.mkdir()
    other=base/'outra-coisa';other.mkdir()
    long_ago=__import__('time').time()-7200
    for d in (old,other):__import__('os').utime(d,(long_ago,long_ago))
    with ident.thread_index():
        pass
    assert not old.exists() and fresh.exists() and other.exists()


def test_cache_dir_must_be_absolute_and_unwritable_base_is_refused(world,monkeypatch,tmp_path):
    ident=world.m.restart_identity
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR','relativo')
    with pytest.raises(ident.Refused,match='absoluto'):
        ident.copy_base()
    blocker=tmp_path/'arquivo';blocker.write_text('x')
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(blocker))
    with pytest.raises(ident.Refused,match='indisponível'):
        ident.copy_base()


def test_live_copy_survives_the_sweep_even_when_old_and_before_sqlite_opens_it(world,monkeypatch):
    # r207/rev-1 P2: idade nao prova abandono; a reserva (flock) da chamada viva protege a copia, tambem antes do connect
    ident=world.m.restart_identity;home=Path(__import__('os').environ['HOME'])
    real=ident.sqlite3.connect
    def connect_after_sweep(path,*a,**k):
        base=ident.copy_base()
        for d in base.iterdir():
            __import__('os').utime(d,(1,1))                  # envelhecida
        now=__import__('time').time()
        monkeypatch.setattr(ident.time,'time',lambda:now+7200)
        ident.sweep_stale(base)
        assert any(d.name.startswith('herdr-restart-index-') for d in base.iterdir())
        return real(path,*a,**k)
    monkeypatch.setattr(ident.sqlite3,'connect',connect_after_sweep)
    with ident.thread_index() as (db,cols):
        assert db.execute('SELECT count(*) FROM threads').fetchone()[0]==3
        base=ident.copy_base()
        assert len(list(base.iterdir()))==1                  # ainda viva durante o consumidor
    assert _copies(home)[1]==[]


def test_old_orphan_with_a_free_reservation_is_removed_but_a_held_one_is_not(world):
    ident=world.m.restart_identity
    base=ident.copy_base();os=__import__('os');time=__import__('time')
    dead=base/'herdr-restart-index-dead';dead.mkdir(mode=0o700)
    held=base/'herdr-restart-index-held';held.mkdir(mode=0o700)
    for d in (dead,held):os.utime(d,(time.time()-7200,)*2)
    fd=ident.hold_copy(held)
    try:
        ident.sweep_stale(base)
        assert not dead.exists() and held.exists()
    finally:os.close(fd)
    ident.sweep_stale(base)
    assert not held.exists()


def test_preexisting_group_or_world_writable_base_is_refused_before_any_copy(world):
    ident=world.m.restart_identity;home=Path(__import__('os').environ['HOME'])
    base=ident.copy_base()
    for mode in (0o777,0o770,0o722):
        base.chmod(mode)
        with pytest.raises(ident.Refused,match='gravável'):
            with ident.thread_index():
                pass
        assert list(base.iterdir())==[]
    base.chmod(0o700);ident.copy_base()
    link=Path(__import__('os').environ['HOME'])/'.cache'/'herdr-mesh-tools'/'tmp'
    link.rmdir();link.symlink_to(home)
    with pytest.raises(ident.Refused):
        ident.copy_base()


def test_writable_ancestor_of_the_cache_is_refused_before_any_copy(world,monkeypatch,tmp_path):
    # r208/rev-1 P2: um pai gravavel por terceiros permite trocar a base 0700 por symlink depois de validada
    ident=world.m.restart_identity;os=__import__('os')
    root=tmp_path/'cache-root';(root/'tmp').mkdir(parents=True,mode=0o700)
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(root))
    root.chmod(0o777)
    with pytest.raises(ident.Refused,match='ancestral.*gravável'):
        with ident.thread_index():
            pass
    assert list((root/'tmp').iterdir())==[]
    root.chmod(0o770)
    with pytest.raises(ident.Refused,match='ancestral.*gravável'):
        ident.copy_base()
    root.chmod(0o1777)                                   # sticky (como /tmp): a troca por terceiros nao e' possivel
    assert ident.copy_base()==root/'tmp'
    root.chmod(0o700)
    with ident.thread_index():
        pass
    assert list((root/'tmp').iterdir())==[]


def test_ancestor_owned_by_another_user_or_symlink_of_a_third_party_is_refused(world,monkeypatch,tmp_path):
    ident=world.m.restart_identity;os=__import__('os')
    root=tmp_path/'dono';(root).mkdir(mode=0o700)
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(root))
    ident.copy_base()
    real=os.getuid()
    monkeypatch.setattr(ident.os,'getuid',lambda:real+4242)     # os diretorios passam a ser "de outro dono"
    with pytest.raises(ident.Refused,match='outro dono|inseguro'):
        ident.copy_base()
    monkeypatch.setattr(ident.os,'getuid',lambda:real)
    target=tmp_path/'alvo';target.mkdir(mode=0o700)
    link=tmp_path/'elo';link.symlink_to(target)
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(link))
    assert ident.copy_base()==link/'tmp'                         # symlink NOSSO para diretorio nosso e' aceito
    real_lstat=Path.lstat
    def lstat(self,*a,**k):
        r=real_lstat(self,*a,**k)
        if self==link:
            return os.stat_result((r.st_mode,r.st_ino,r.st_dev,r.st_nlink,real+4242,r.st_gid,r.st_size,0,0,0))
        return r
    monkeypatch.setattr(Path,'lstat',lstat)
    with pytest.raises(ident.Refused,match='symlink de terceiro'):
        ident.copy_base()


def test_our_symlink_to_a_private_dir_under_a_writable_physical_parent_is_refused(world,monkeypatch,tmp_path):
    # r209: so o alvo final do symlink era inspecionado; o pai fisico gravavel do alvo passava
    ident=world.m.restart_identity;os=__import__('os')
    pub=tmp_path/'publico';pub.mkdir();pub.chmod(0o777)
    dest=pub/'privado';dest.mkdir(mode=0o700)
    link=tmp_path/'elo';link.symlink_to(dest)
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(link))
    with pytest.raises(ident.Refused,match='gravável'):
        with ident.thread_index():
            pass
    assert list((dest/'tmp').iterdir())==[]                  # nada foi copiado
    pub.chmod(0o700)
    assert ident.copy_base()==link/'tmp'                    # cadeia toda confiavel: aceito


def test_relative_and_chained_links_are_followed_and_an_unsafe_middle_hop_is_refused(world,monkeypatch,tmp_path):
    ident=world.m.restart_identity
    safe=tmp_path/'seguro';safe.mkdir(mode=0o700);final=safe/'final';final.mkdir(mode=0o700)
    bad=tmp_path/'ruim';bad.mkdir();bad.chmod(0o777)
    (bad/'meio').symlink_to(final)                          # elo intermediario dentro de diretorio gravavel
    first=tmp_path/'primeiro';first.symlink_to('ruim/meio')   # destino relativo
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(first))
    with pytest.raises(ident.Refused,match='gravável'):
        ident.copy_base()
    bad.chmod(0o700)
    assert ident.copy_base()==first/'tmp'                   # encadeado e relativo, tudo confiavel
    loop_a=tmp_path/'a';loop_b=tmp_path/'b'
    loop_a.symlink_to('b');loop_b.symlink_to('a')
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(loop_a))
    with pytest.raises(ident.Refused):
        ident.copy_base()
    monkeypatch.setenv('HERDR_MESH_CACHE_DIR',str(tmp_path/'x'/'..'/'y'))
    with pytest.raises(ident.Refused,match='\\.\\.'):
        ident.copy_base()
