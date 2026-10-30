"""No real panes or keys: startup confirmation is an expiring one-shot capability."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import _herdr_startup as startup

MENU = 'Where should the new conversation run?\n› 1. Use current Git worktree\n  2. Create new worktree\n'


@pytest.fixture
def env(tmp_path):
    info = {'name':'sample-rev-1', 'pane_id':'w:p2', 'terminal_id':'term-new', 'agent':'codex',
            'agent_status':'blocked', 'cwd':str(tmp_path), 'foreground_cwd':str(tmp_path),
            'revision':2, 'state_change_seq':8, 'agent_session':None}
    calls = []; screens = [MENU, MENU]
    def get(target): return dict(info)
    def read(pane):
        calls.append(('read', pane)); return screens.pop(0) if screens else MENU
    def key(pane): calls.append(('key', pane)); return True
    runtime = SimpleNamespace(get_agent_info=get)
    receipt = startup.NewPane('w:p2','term-new',str(tmp_path),startup.time.monotonic())
    return info, calls, screens, runtime, receipt, read, key


def confirm(env, **kwargs):
    _, _, _, runtime, receipt, read, key = env
    return startup.confirm_new_conversation_dialog(runtime, receipt, name='sample-rev-1',
                                                   kind='codex', read=read, send_enter=key, **kwargs)


def test_codex_new_pane_selected_option_one_is_confirmed_once(env):
    assert confirm(env) is True
    assert env[1].count(('key','w:p2')) == 1
    assert confirm(env) is False
    assert env[1].count(('key','w:p2')) == 1


@pytest.mark.parametrize('screen', [MENU.replace('› 1.', '  1.').replace('  2.', '› 2.'),
                                  MENU.replace('› ', '  '), 'ordinary compose draft',
                                  MENU + '› 2. Create new worktree\n'])
def test_unselected_or_ambiguous_dialog_never_presses_enter(env, screen):
    env[2][:] = [screen, screen]
    assert confirm(env) is False
    assert not any(c[0]=='key' for c in env[1])


def test_selection_change_between_reads_never_presses_enter(env):
    env[2][:] = [MENU, MENU.replace('› 1.', '  1.').replace('  2.', '› 2.')]
    assert confirm(env) is False
    assert not any(c[0]=='key' for c in env[1])


def test_claude_trust_is_refused_with_pane_and_text_no_key(env):
    info, calls, screens, runtime, receipt, read, key = env
    info['agent'] = 'claude'; screen='Do you trust this folder?\n  1. Yes, I trust this folder\n› 2. No, exit'
    screens[:] = [screen]
    with pytest.raises(startup.StartupDialogRefused) as exc:
        startup.inspect_blocked_start(runtime, receipt, pane='w:p2', name='sample-rev-1',
                                      kind='claude', read=read, send_enter=key)
    assert 'w:p2' in str(exc.value) and 'No, exit' in str(exc.value)
    assert not any(c[0]=='key' for c in calls)


@pytest.mark.parametrize('change', ['kind','terminal','session','expired','working'])
def test_stale_or_replaced_startup_identity_never_presses_enter(env, change):
    info, calls, _, _, receipt, _, _ = env
    if change == 'kind': info['agent']='claude'
    elif change == 'terminal': info['terminal_id']='replacement'
    elif change == 'session':
        old_get = env[3].get_agent_info; count=[]
        def get(target):
            count.append(1); result=old_get(target)
            if len(count)>1: result['agent_session']={'value':'replacement'}
            return result
        env[3].get_agent_info=get
    elif change == 'expired': receipt.created_at -= 1000
    else: info['agent_status']='working'
    assert confirm(env) is False
    assert not any(c[0]=='key' for c in calls)


def test_no_new_pane_capability_means_no_confirmation(env):
    _, calls, _, runtime, _, read, key=env
    assert startup.confirm_new_conversation_dialog(runtime,None,name='sample-rev-1',kind='codex',
                                                   read=read,send_enter=key) is False
    assert not calls


def test_read_command_is_agent_detection_text_and_key_is_logical_enter(env, monkeypatch):
    _, _, _, runtime, receipt, _, _ = env
    commands=[]
    runtime.herdr_argv=lambda *args:['herdr',*args]
    def run(command, **kw):
        commands.append(command)
        return SimpleNamespace(returncode=0,stdout=MENU,stderr='')
    monkeypatch.setattr(startup.subprocess,'run',run)
    assert startup.confirm_new_conversation_dialog(runtime,receipt,name='sample-rev-1',kind='codex')
    assert commands[0]==['herdr','agent','read','w:p2','--source','detection','--format','text']
    assert commands[-1]==['herdr','agent','send-keys','w:p2','enter']


def test_creation_receipt_requires_empty_physical_pane_and_terminal_identity(tmp_path):
    info={'pane_id':'w:p2','terminal_id':'term-new','agent':None}
    runtime=SimpleNamespace(api=lambda *a:{'pane':dict(info)})
    receipt=startup.created_pane_receipt(runtime,{'pane_id':'w:p2'},tmp_path)
    assert receipt.pane_id=='w:p2' and receipt.terminal_id=='term-new'
    info['agent']='claude'
    assert startup.created_pane_receipt(runtime,{'pane_id':'w:p2'},tmp_path) is None
    info['agent']=None;info.pop('terminal_id')
    assert startup.created_pane_receipt(runtime,{'pane_id':'w:p2'},tmp_path) is None


def test_enter_failure_consumes_receipt_and_does_not_retry(env):
    _, calls, _, runtime, receipt, read, _=env
    def fail(pane): calls.append(('key',pane));raise OSError('unknown delivery')
    assert startup.confirm_new_conversation_dialog(runtime,receipt,name='sample-rev-1',kind='codex',
                                                  read=read,send_enter=fail) is False
    assert receipt.consumed
    assert confirm(env) is False and calls.count(('key','w:p2'))==1


@pytest.mark.parametrize('field', ['revision','state_change_seq'])
@pytest.mark.parametrize('value', [None, 'missing', '2', True, -1])
def test_missing_or_invalid_revision_sequence_refuses_without_key(env, field, value):
    if value == 'missing': env[0].pop(field)
    else: env[0][field]=value
    assert confirm(env) is False and not any(c[0]=='key' for c in env[1])


def test_receipt_expiring_during_reads_is_not_consumed_or_sent(env, monkeypatch):
    info,calls,screens,runtime,receipt,read,key=env; clock=[179.0];receipt.created_at=0
    monkeypatch.setattr(startup.time,'monotonic',lambda:clock[0])
    def delayed(pane): clock[0]+=2;return MENU
    assert startup.confirm_new_conversation_dialog(runtime,receipt,name='sample-rev-1',kind='codex',
                                                  read=delayed,send_enter=key) is False
    assert not receipt.consumed and not any(c[0]=='key' for c in calls)


def test_known_current_worktree_description_is_accepted_but_other_suffix_is_not(env):
    screen=MENU.replace('› 1. Use current Git worktree','› 1. Use current Git worktree  Keep using the current working directory')
    assert startup.selected_current_worktree(screen)
    assert not startup.selected_current_worktree(screen.replace('Keep using the current working directory',
                                                               'Create a separate checkout'))


@pytest.mark.parametrize('exc', [OSError('read failed'), startup.subprocess.TimeoutExpired('read',10)])
def test_inspection_read_failure_is_clear_refusal_without_key(env, exc):
    _,calls,_,runtime,receipt,_,key=env
    def unreadable(pane): raise exc
    with pytest.raises(startup.StartupDialogRefused, match='w:p2'):
        startup.inspect_blocked_start(runtime,receipt,pane='w:p2',name='sample-rev-1',kind='claude',
                                      read=unreadable,send_enter=key)
    assert not calls


def test_invalid_created_pane_metadata_returns_no_receipt():
    runtime=SimpleNamespace(api=lambda *a:{'pane':None})
    assert startup.created_pane_receipt(runtime,{'pane_id':'w:p2'},'/arbitrary') is None


def test_observed_codex_0164_pane_fixture_is_recognized():
    # Supplied over herdr-message by herdr-exec; real tool result of26/10,
    # <id omitido>. No live session reset was performed.
    fixture=Path(__file__).parent/'fixtures/startup/codex-0.164-new-conversation.txt'
    assert startup.selected_current_worktree(fixture.read_text())
