"""Goal snapshots never authorize identity or guarantee atomic goal admission."""
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import _herdr_message_goals as module

SID = '00000000-0000-4000-8000-000000000001'
GID = '00000000-0000-4000-8000-000000000002'


def codex(root, status='complete'):
    root.mkdir(exist_ok=True)
    db = sqlite3.connect(root / 'goals_1.sqlite')
    db.execute('''CREATE TABLE thread_goals(thread_id TEXT,goal_id TEXT,objective TEXT,status TEXT,
        token_budget INTEGER,tokens_used INTEGER,time_used_seconds INTEGER,created_at_ms INTEGER,updated_at_ms INTEGER)''')
    db.execute('INSERT INTO thread_goals VALUES (?,?,?,?,?,?,?,?,?)',
               (SID,GID,'PRIVATE OBJECTIVE',status,None,0,0,1,1))
    db.commit();db.close()


def claude(root, rows):
    p=root/'projects'/'test-project'/(SID+'.jsonl');p.parent.mkdir(parents=True)
    p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return p


def event(met,**extra):
    return {'type':'attachment','sessionId':SID,'attachment':{'type':'goal_status','met':met,'sentinel':True,'condition':'PRIVATE CONDITION',**extra}}


@pytest.mark.parametrize('native,expected',[('active','protected'),('paused','protected'),('blocked','protected'),('complete','clear'),('new-status','unknown')])
def test_codex_states_and_private_text(tmp_path,native,expected):
    codex(tmp_path,native)
    before=(tmp_path/'goals_1.sqlite').read_bytes()
    observed=module.read_goal('codex',tmp_path,SID,binding_verified=True)
    assert observed['status']==expected
    assert 'PRIVATE' not in json.dumps(observed)
    assert (tmp_path/'goals_1.sqlite').read_bytes()==before


def test_source_cannot_assert_its_own_session_binding(tmp_path):
    codex(tmp_path)
    assert module.read_goal('codex',tmp_path,SID)['status']=='unknown'


@pytest.mark.parametrize('kind',['codex','claude','unsupported'])
def test_missing_source_or_session_is_unknown(tmp_path,kind):
    assert module.read_goal(kind,tmp_path,None,binding_verified=True)['status']=='unknown'
    assert module.read_goal(kind,tmp_path,SID,binding_verified=True)['status']=='unknown'
    assert not list(tmp_path.iterdir())


def test_codex_missing_row_does_not_mean_unbound_thread_is_safe(tmp_path):
    codex(tmp_path)
    other=str(uuid.uuid4())
    assert module.read_goal('codex',tmp_path,other)['status']=='unknown'
    assert module.read_goal('codex',tmp_path,other,binding_verified=True)['status']=='clear'


def test_bad_codex_schema_fails_closed(tmp_path):
    db=sqlite3.connect(tmp_path/'goals_1.sqlite');db.execute('CREATE TABLE thread_goals(thread_id TEXT,status TEXT)');db.close()
    assert module.read_goal('codex',tmp_path,SID,binding_verified=True)['status']=='unknown'


@pytest.mark.parametrize('met,expected',[(False,'protected'),(True,'clear'),('false','unknown'),(0,'unknown')])
def test_claude_native_goal_status_and_clear_marker(tmp_path,met,expected):
    claude(tmp_path,[event(False),event(met)])
    observed=module.read_goal('claude',tmp_path,SID,binding_verified=True)
    assert observed['status']==expected and 'PRIVATE' not in json.dumps(observed)


def test_clear_new_session_never_uses_old_session_file(tmp_path):
    claude(tmp_path,[event(False)])
    new=str(uuid.uuid4());other=tmp_path/'projects'/'test-project'/(new+'.jsonl')
    other.write_text(json.dumps({'type':'system','sessionId':new})+'\n')
    assert module.read_goal('claude',tmp_path,new,binding_verified=True)['status']=='unknown'
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']=='protected'


@pytest.mark.parametrize('tail',['{','[]\n','null\n'])
def test_partial_or_malformed_jsonl_cannot_hide_new_goal(tmp_path,tail):
    p=claude(tmp_path,[event(True)]);p.write_text(p.read_text()+tail)
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']=='unknown'


def test_ambiguous_transcript_is_unknown(tmp_path):
    p=claude(tmp_path,[event(True)]);q=tmp_path/'projects'/'copy'/p.name;q.parent.mkdir();q.write_bytes(p.read_bytes())
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']=='unknown'


def test_source_replacement_during_json_parse_is_unknown(tmp_path,monkeypatch):
    p=claude(tmp_path,[event(True)]);loads=module.json.loads
    def change(text):
        result=loads(text);p.write_text(p.read_text()+json.dumps(event(False))+'\n');return result
    monkeypatch.setattr(module.json,'loads',change)
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']=='unknown'


@pytest.mark.parametrize('kind',['codex','claude'])
def test_symlink_sources_refuse(tmp_path,kind):
    root=tmp_path/'root';root.mkdir()
    if kind=='codex':
        codex(tmp_path/'real');(root/'goals_1.sqlite').symlink_to(tmp_path/'real/goals_1.sqlite')
    else:
        original=claude(tmp_path/'real',[event(True)]);folder=root/'projects'/'project';folder.mkdir(parents=True);(folder/original.name).symlink_to(original)
    assert module.read_goal(kind,root,SID,binding_verified=True)['status']=='unknown'

@pytest.mark.parametrize('attachment',[None,{}, {'type':'goal_status_v2','met':False}])
def test_unrecognized_attachment_cannot_hide_a_new_goal(tmp_path,attachment):
    claude(tmp_path,[event(True),{'type':'attachment','attachment':attachment}])
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']=='unknown'


def test_wrong_transcript_session_is_unknown(tmp_path):
    row=event(True);row['sessionId']=str(uuid.uuid4());claude(tmp_path,[row])
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']=='unknown'


def test_source_size_limit_refuses_without_reading_body(tmp_path,monkeypatch):
    claude(tmp_path,[event(True)]);monkeypatch.setattr(module,'MAX_SOURCE_BYTES',1)
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']=='unknown'


def test_duplicate_goal_rows_are_unknown(tmp_path):
    codex(tmp_path);db=sqlite3.connect(tmp_path/'goals_1.sqlite');db.execute('INSERT INTO thread_goals SELECT * FROM thread_goals');db.commit();db.close()
    assert module.read_goal('codex',tmp_path,SID,binding_verified=True)['status']=='unknown'

@pytest.mark.parametrize('event_type',['goal_status_v2','goal_started','thread_goal_changed'])
def test_new_top_level_goal_event_cannot_preserve_old_clear(tmp_path,event_type):
    claude(tmp_path,[event(True),{'type':event_type,'met':False,'condition':'NEW PRIVATE GOAL'}])
    result=module.read_goal('claude',tmp_path,SID,binding_verified=True)
    assert result['status']=='unknown' and 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('separator',['\u2028','\u2029','\x85'])
@pytest.mark.parametrize('met,expected',[(False,'protected'),(True,'clear')])
def test_unicode_inside_json_record_is_not_a_record_separator(tmp_path,separator,met,expected):
    p=claude(tmp_path,[event(met)])
    with p.open('a') as f:f.write(json.dumps({'type':'user','message':'text'+separator+'goal_status_v2'},ensure_ascii=False)+'\n')
    assert module.read_goal('claude',tmp_path,SID,binding_verified=True)['status']==expected


def inventory(root):
    return {p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize('mode',['closed','live','missing_shm','orphan_shm'])
def test_wal_source_never_touches_main_or_sidecars(tmp_path,mode):
    codex(tmp_path,'active');writer=sqlite3.connect(tmp_path/'goals_1.sqlite')
    writer.execute('PRAGMA journal_mode=WAL');writer.execute('UPDATE thread_goals SET tokens_used=1');writer.commit()
    if mode!='live':writer.close()
    if mode=='missing_shm':
        # A copied live WAL without SHM is a valid refusal fixture.
        (tmp_path/'goals_1.sqlite-wal').write_bytes(b'not-a-complete-wal')
    if mode=='orphan_shm':(tmp_path/'goals_1.sqlite-shm').write_bytes(b'fixture')
    before=inventory(tmp_path)
    try:
        result=module.read_goal('codex',tmp_path,SID,binding_verified=True)
        assert result['status']=='unknown'  # WAL support deferred, never use stale main.
        assert inventory(tmp_path)==before
    finally:
        if mode=='live':writer.close()


def test_sqlite_engine_only_receives_memory_not_source_paths(tmp_path,monkeypatch):
    codex(tmp_path);connect=module.sqlite3.connect;calls=[]
    def memory_only(name,*args,**kwargs):
        calls.append(name)
        assert name==':memory:','SQLite VFS must never open the live config'
        return connect(name,*args,**kwargs)
    monkeypatch.setattr(module.sqlite3,'connect',memory_only)
    assert module.read_goal('codex',tmp_path,SID,binding_verified=True)['status']=='clear'
    assert calls==[':memory:']
