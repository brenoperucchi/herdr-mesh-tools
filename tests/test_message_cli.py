import importlib.util
from importlib.machinery import SourceFileLoader
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from _herdr_message_ledger import Ledger, MessageError
from test_message_ledger import endpoint

path = Path(__file__).resolve().parents[1] / 'bin/herdr-message'
loader = SourceFileLoader('message_cli_test', str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


@pytest.fixture
def env(tmp_path):
    with Ledger(tmp_path/'state', 'test') as db:
        rt = SimpleNamespace(caller=lambda: endpoint('a'), get=lambda _: endpoint('b'))
        yield db, rt


def test_send_does_no_transport_io_and_returns_queued(env, monkeypatch, tmp_path):
    db, rt = env
    monkeypatch.setattr(cli, 'healthy', lambda _: True)
    file = tmp_path/'body'
    file.write_text('Build ryzen9')
    args = SimpleNamespace(command='send', target='b', subject='Build', file=str(file),
                           key='k', notice=False, send_timeout=900, reply_timeout=900)
    out = cli.execute(args, db, rt)
    assert out['code'] == 'queued'
    assert db.get(out['id'])['transport'] == 'queued'
    # Injected runtime has no prompt, sleep, wait, send-keys or submit methods.


def test_unavailable_supervisor_refuses_acceptance(env, monkeypatch):
    db, rt = env
    monkeypatch.setattr(cli, 'healthy', lambda _: False)
    with pytest.raises(MessageError, match='worker_unavailable'):
        cli.execute(SimpleNamespace(command='send'), db, rt)
    assert db.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0


def test_reply_is_saved_even_without_running_supervisor(env, monkeypatch, tmp_path):
    db, rt = env
    req = db.send(endpoint('a'), endpoint('b'), 'Build', 'Do it', 'k')
    rt.caller = lambda: endpoint('b')
    monkeypatch.setattr(cli, 'healthy', lambda _: False)
    p = tmp_path/'reply'
    p.write_text('Ready')
    result = cli.execute(SimpleNamespace(command='reply', id=req['id'], file=str(p), key='r'), db, rt)
    assert result['code'] == 'reply_recorded'
    assert not result['worker_healthy']
    assert db.body(result['id']) == 'Ready'


def test_nonparticipant_cannot_see_conversation_or_body(env):
    db, rt = env
    req = db.send(endpoint('a'), endpoint('b'), 'Build', 'secret', 'k')
    rt.caller = lambda: endpoint('c')
    for command in ('status', 'show'):
        with pytest.raises(MessageError, match='not_owned'):
            cli.execute(SimpleNamespace(command=command, id=req['id']), db, rt)


def test_no_wait_option_is_exposed():
    with pytest.raises(SystemExit) as exc:
        cli.main(['send', 'b', '--subject', 'x', '--key', 'k', '--stdin', '--wait'])
    assert exc.value.code == 2
def test_sqlite_error_detail_names_the_failure_but_never_leaks_payload(tmp_path, monkeypatch, capsys):
    """11:27-11:30 de 18/11: `{"detail":"OperationalError"}` sem dizer se era trava ou corrupcao."""
    import sqlite3
    rt = SimpleNamespace(caller=lambda: endpoint('a'), scope='test')
    monkeypatch.setattr(cli, 'Runtime', lambda **kw: rt)

    def locked(*a, **k):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(cli, 'Ledger', locked)
    assert cli.main(['--state-dir', str(tmp_path / 's'), 'inbox']) == 1
    err = capsys.readouterr().err
    assert 'OperationalError: database is locked' in err


def test_non_sqlite_oserror_detail_stays_type_only(tmp_path, monkeypatch, capsys):
    rt = SimpleNamespace(caller=lambda: endpoint('a'), scope='test')
    monkeypatch.setattr(cli, 'Runtime', lambda **kw: rt)

    def boom(*a, **k):
        raise OSError('/home/secret/path with body text')
    monkeypatch.setattr(cli, 'Ledger', boom)
    assert cli.main(['--state-dir', str(tmp_path / 's'), 'inbox']) == 1
    err = capsys.readouterr().err
    assert 'OSError' in err and 'secret' not in err
