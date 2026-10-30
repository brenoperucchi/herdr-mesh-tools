"""Security configuration writes use only a synthetic HOME and project table."""
import json
from pathlib import Path
import sys
import tomllib

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import _herdr_trust as trust


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    project = home / 'Devs/demo'; project.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    monkeypatch.setattr(trust, 'git_root', lambda path: None)
    monkeypatch.setattr(trust, 'remote_warning', lambda path: None)
    return trust.TrustStore(home=home, registered=[project]), project


def write(store, kind, value):
    path = store.config_path(kind); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


@pytest.mark.parametrize('kind', ['codex', 'claude'])
def test_registered_project_is_trusted_idempotently_with_backup(store, kind):
    obj, project = store
    before = '# retained\nmodel = "fixture"\n' if kind == 'codex' else '{"other": {"keep": 7}}\n'
    path = write(obj, kind, before)
    result = obj.ensure_trusted(project, kind)
    assert result['status'] == 'trusted'
    assert Path(result['backup']).read_text() == before
    actual = path.read_bytes(); backups = list((obj.home / '.local/state/herdr-trust/backups').glob('*.bak'))
    assert obj.ensure_trusted(project, kind)['status'] == 'already_trusted'
    assert path.read_bytes() == actual and list((obj.home / '.local/state/herdr-trust/backups').glob('*.bak')) == backups
    if kind == 'claude': assert json.loads(path.read_text())['other'] == {'keep': 7}


def test_toml_comments_order_and_other_project_options_survive(store):
    obj, project = store
    text = '# header\nmodel = "fixture" # model comment\n\n[projects.' + json.dumps(str(project)) + '] # project comment\n# trust comment\nkeep = 42\n\n[other]\nx = "retained"\n'
    path = write(obj, 'codex', text)
    assert obj.ensure_trusted(project, 'codex')['status'] == 'trusted'
    assert path.read_text() == text.replace('] # project comment\n', '] # project comment\ntrust_level = "trusted"\n')
    assert tomllib.loads(path.read_text())['projects'][str(project)]['keep'] == 42


def test_codex_skips_when_git_root_is_already_trusted(store, monkeypatch):
    obj, project = store; root = obj.home / 'Devs'
    path = write(obj, 'codex', '[projects.' + json.dumps(str(root)) + ']\ntrust_level="trusted"\n')
    before = path.read_bytes()
    monkeypatch.setattr(trust, 'git_root', lambda p: root)
    assert obj.ensure_trusted(project, 'codex')['status'] == 'already_trusted'
    assert path.read_bytes() == before and not list(path.parent.glob('*.bak'))


@pytest.mark.parametrize('name', ['outside', 'scratchpad'])
@pytest.mark.parametrize('kind', ['codex', 'claude'])
def test_unregistered_or_scratch_directory_is_refused_without_config_writes(store, name, kind):
    obj, project = store
    target = obj.home / name; target.mkdir()
    if name == 'scratchpad': obj.registered.add(str(target))
    assert obj.ensure_trusted(target, kind)['status'] == 'refused'
    assert not obj.config_path(kind).exists()


def test_root_home_tmp_and_tmp_symlink_are_refused_even_if_registered(store, tmp_path):
    obj, project = store
    alias = tmp_path / 'alias'; alias.symlink_to(project, target_is_directory=True)
    for path in (Path('/'), obj.home, Path('/tmp'), alias):
        obj.registered.add(str(path.resolve()))
        assert obj.ensure_trusted(path, 'codex')['status'] == 'refused'
    assert not obj.config_path('codex').exists()


def test_claude_race_retries_from_latest_and_preserves_other_keys(store, monkeypatch):
    obj, project = store
    path = write(obj, 'claude', '{"counter":1}')
    original = trust.replace_if_unchanged; calls = []
    def race(p, baseline, data):
        calls.append(1)
        if len(calls) == 1:
            p.write_text('{"counter":2,"new_key":"keep"}')
        return original(p, baseline, data)
    monkeypatch.setattr(trust, 'replace_if_unchanged', race)
    assert obj.ensure_trusted(project, 'claude')['status'] == 'trusted'
    assert len(calls) == 2
    data = json.loads(path.read_text())
    assert data['counter'] == 2 and data['new_key'] == 'keep'
    assert data['projects'][str(project)]['hasTrustDialogAccepted'] is True


def test_repeated_race_stops_after_two_attempts_without_clobber(store, monkeypatch):
    obj, project = store
    path = write(obj, 'claude', '{"counter":0}')
    original = trust.replace_if_unchanged; calls = []
    def race(p, baseline, data):
        calls.append(1); p.write_text(json.dumps({'counter': len(calls)}))
        return original(p, baseline, data)
    monkeypatch.setattr(trust, 'replace_if_unchanged', race)
    assert obj.ensure_trusted(project, 'claude')['status'] == 'warning'
    assert len(calls) == 2 and json.loads(path.read_text()) == {'counter': 2}


def test_claude_post_write_overwrite_is_retried_once(store, monkeypatch):
    obj, project = store
    path = write(obj, 'claude', '{"counter":0}')
    original = trust.replace_if_unchanged; calls = []
    def race(p, baseline, data):
        original(p, baseline, data); calls.append(1)
        if len(calls) == 1: p.write_text('{"counter":1}')
    monkeypatch.setattr(trust, 'replace_if_unchanged', race)
    assert obj.ensure_trusted(project, 'claude')['status'] == 'trusted'
    assert len(calls) == 2 and json.loads(path.read_text())['counter'] == 1


@pytest.mark.parametrize('kind,text', [('codex', '[invalid'), ('claude', '{invalid')])
def test_invalid_configuration_is_untouched(store, kind, text):
    obj, project = store; path = write(obj, kind, text)
    assert obj.ensure_trusted(project, kind)['status'] == 'warning'
    assert path.read_text() == text


def test_config_symlink_is_not_replaced_or_followed_for_writes(store):
    obj, project = store; target = obj.home / 'unrelated'; target.write_text('{}')
    path = obj.config_path('claude'); path.symlink_to(target)
    assert obj.ensure_trusted(project, 'claude')['status'] == 'warning'
    assert path.is_symlink() and target.read_text() == '{}'


def test_audit_is_read_only_lists_missing_and_outside(store):
    obj, project = store
    outside = obj.home / 'old'
    write(obj, 'claude', json.dumps({'projects': {str(outside): {'hasTrustDialogAccepted': True}}}))
    before = obj.config_path('claude').read_bytes()
    report = obj.audit()
    assert any(e['directory'] == str(project) and e['status'] == 'missing' for e in report['entries'])
    assert any(e['directory'] == str(outside) for e in report['outside'])
    assert obj.config_path('claude').read_bytes() == before and not obj.config_path('codex').exists()


def test_registry_reader_does_not_execute_bootstrap_code(tmp_path):
    source = tmp_path / 'bootstrap'
    source.write_text('raise Exception("must not execute")\nSPACES = [("demo", f"{HOME}/Devs/demo", None, None)]\n')
    assert trust.registered_directories(source, tmp_path) == {str(tmp_path / 'Devs/demo')}


def test_initial_read_race_also_has_one_retry(store, monkeypatch):
    obj, project = store; write(obj, 'claude', '{}')
    original = trust.read_regular; calls = []
    def read(path):
        calls.append(1)
        if len(calls) == 1: raise trust.ConcurrentWrite('initial read raced')
        return original(path)
    monkeypatch.setattr(trust, 'read_regular', read)
    assert obj.ensure_trusted(project, 'claude')['status'] == 'trusted'
    assert len(calls) > 1


def test_codex_config_parent_symlink_cannot_escape_synthetic_home(store):
    obj, project = store
    target = obj.home / 'elsewhere'; target.mkdir()
    (target / 'config.toml').write_text('# retain\n')
    (obj.home / '.codex').symlink_to(target, target_is_directory=True)
    assert obj.ensure_trusted(project, 'codex')['status'] == 'warning'
    assert (target / 'config.toml').read_text() == '# retain\n'


def test_third_party_origin_is_warning_not_refusal_and_url_credentials_not_leaked(store, monkeypatch):
    obj, project = store
    monkeypatch.setattr(trust, 'remote_warning', lambda p: 'origin fora do Breno; hooks/MCP')
    result = obj.ensure_trusted(project, 'codex')
    assert result['status'] == 'trusted' and result['warnings']


def load_sync():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader('trust_sync_test', str(Path(trust.__file__).with_name('herdr-trust-sync')))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec); loader.exec_module(module)
    return module


def test_sync_defaults_to_audit_without_writes(store, monkeypatch, capsys):
    obj, project = store; module = load_sync()
    monkeypatch.setattr(module.trust, 'TrustStore', lambda: obj)
    monkeypatch.setattr(sys, 'argv', ['herdr-trust-sync'])
    monkeypatch.setattr(trust, 'replace_if_unchanged', lambda *a: pytest.fail('audit attempted write'))
    assert module.main() == 0
    assert any(e['status'] == 'missing' for e in json.loads(capsys.readouterr().out)['entries'])
    assert not obj.config_path('claude').exists() and not obj.config_path('codex').exists()


def test_sync_apply_only_registered_missing_project_preserves_outside(store, monkeypatch, capsys):
    obj, project = store; module = load_sync(); outside = str(obj.home / 'old')
    write(obj, 'claude', json.dumps({'projects': {outside: {'hasTrustDialogAccepted': False}}}))
    monkeypatch.setattr(module.trust, 'TrustStore', lambda: obj)
    monkeypatch.setattr(sys, 'argv', ['herdr-trust-sync', '--apply'])
    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result['applied']) == 2
    after = json.loads(obj.config_path('claude').read_text())
    assert after['projects'][outside]['hasTrustDialogAccepted'] is False
    assert after['projects'][str(project)]['hasTrustDialogAccepted'] is True


def test_registry_default_environment_entry_supported_but_override_never_trusted(tmp_path, monkeypatch):
    source = tmp_path / 'bootstrap'
    source.write_text('SPACES = [("a", f"{HOME}/Devs/a", None), ("MFC", os.environ.get("HERDR_MFC_CWD", f"{HOME}/Devs/mfc"), None)]')
    monkeypatch.delenv('HERDR_MFC_CWD', raising=False)
    assert len(trust.registered_directories(source, tmp_path)) == 2
    monkeypatch.setenv('HERDR_MFC_CWD', '/arbitrary/override')
    refused = []
    assert trust.registered_directories(source, tmp_path, refused) == {str(tmp_path / 'Devs/a')}
    assert len(refused) == 1 and refused[0]['label'] == 'MFC'
    assert '/arbitrary/override' not in str(refused)


def test_real_registry_shape_is_readable_without_importing_it(tmp_path, monkeypatch):
    monkeypatch.delenv('HERDR_MFC_CWD', raising=False)
    refused = []
    result = trust.registered_directories(Path(trust.__file__).with_name('herdr-bootstrap'), tmp_path, refused)
    assert len(result) > 10 and str(tmp_path / 'Devs/mfc') in result and not refused


def test_explicit_untrusted_is_reported_and_never_overridden(store, monkeypatch, capsys):
    obj, project = store
    text = '[projects.' + json.dumps(str(project)) + ']\ntrust_level = "untrusted" # explicit decision\n'
    path = write(obj, 'codex', text)
    assert obj.ensure_trusted(project, 'codex')['status'] == 'explicitly_untrusted'
    assert any(e['status'] == 'explicitly_untrusted' for e in obj.audit()['entries'])
    module = load_sync(); monkeypatch.setattr(module.trust, 'TrustStore', lambda:obj)
    monkeypatch.setattr(sys, 'argv', ['herdr-trust-sync','--apply'])
    assert module.main() == 0
    assert path.read_text() == text


def test_multiple_projects_and_race_keep_one_backup_per_file_per_execution(store):
    obj, project = store
    second = obj.home / 'Devs/second'; second.mkdir(); obj.registered.add(str(second))
    path = write(obj, 'claude', '{"counter":0}')
    first = obj.ensure_trusted(project, 'claude'); next_result = obj.ensure_trusted(second, 'claude')
    assert first['backup'] == next_result['backup']
    assert len(list((obj.home / '.local/state/herdr-trust/backups').glob('*.bak'))) == 1
    assert not list(path.parent.glob('*.lock'))


@pytest.mark.parametrize('url,warning', [
    ('https://user:SUPER_SECRET@github.com/brenoperucchi/demo.git', False),
    ('git@github.com:brenoperucchi/demo.git', False),
    ('ssh://git@github.com:2222/brenoperucchi/demo.git', False),
    ('https://user:SUPER_SECRET@github.com/third-party/demo.git', True),
    ('https://github.com.evil.com/brenoperucchi/demo.git', True),
    ('/local/repo', True),
])
def test_remote_owner_parsing_and_credentials_redaction(monkeypatch, url, warning):
    import subprocess
    monkeypatch.setattr(trust.subprocess, 'run', lambda *a,**k:subprocess.CompletedProcess([],0,url,''))
    result = trust.remote_warning('/unused')
    assert bool(result) == warning and 'SUPER_SECRET' not in str(result) and url not in str(result)


@pytest.mark.parametrize('alias_kind', ['temporary','scratchpad'])
def test_registered_alias_cannot_be_laundered_by_audit_apply(store, tmp_path, monkeypatch, capsys, alias_kind):
    obj, project = store
    alias = tmp_path / 'alias' if alias_kind == 'temporary' else obj.home / 'scratchpad/alias'
    alias.parent.mkdir(parents=True, exist_ok=True); alias.symlink_to(project, target_is_directory=True)
    obj = trust.TrustStore(home=obj.home, registered=[alias])
    report = obj.audit()
    assert not any(e['status']=='missing' for e in report['entries'])
    assert any(e['status']=='refused' for e in report['entries'])
    module=load_sync(); monkeypatch.setattr(module.trust,'TrustStore',lambda:obj)
    monkeypatch.setattr(sys,'argv',['herdr-trust-sync','--apply'])
    monkeypatch.setattr(trust,'replace_if_unchanged',lambda *a:pytest.fail('forbidden alias wrote config'))
    assert module.main()==1
    assert not obj.config_path('codex').exists() and not obj.config_path('claude').exists()
