import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import _herdr_review_policy as policy


def test_level_one_requires_real_justification(tmp_path):
    for reason in (None, '', '  '):
        with pytest.raises(RuntimeError, match='justificativa'):
            policy.evaluate(str(tmp_path), level=1, justification=reason, files=['readme.txt'])
    result = policy.evaluate(str(tmp_path), level=1, justification='correção textual', files=['readme.txt'])
    assert result['nivel'] == 1 and result['justificativa'] == 'correção textual'


@pytest.mark.parametrize('path', ['.env.local', 'nested/.env', 'cert.key', 'secrets/token',
    'migrations/001.sql', 'bin/herdr-migrate-rev', 'systemd/worker.service.example',
    'deploy/release', 'gateway/config.py', 'protocol/message.py', 'schema.sql',
    'bin/herdr-review-dispatch', 'bin/_herdr_trust.py', 'bin/_herdr_startup.py',
    'bin/herdr-trust-sync', 'bin/herdr-bootstrap', 'bin/herdr-add-space', 'bin/herdr-rename-space',
    'bin/_herdr_review_complement.py', 'bin/_herdr_review_groups.py',
    'bin/_herdr_dispatch.py', 'bin/_herdr_message_ledger.py',
    '.herdr/review-gatilhos.json'])
def test_global_trigger_refuses_level_one(tmp_path, path):
    with pytest.raises(RuntimeError, match='nível 2 obrigatório'):
        policy.evaluate(str(tmp_path), level=1, justification='pequena', files=[path])


@pytest.mark.parametrize('project', ['Guardian', 'mfc', 'cerebro'])
def test_private_project_root_is_trigger(tmp_path, project):
    (tmp_path / project).mkdir()
    with pytest.raises(RuntimeError, match='privado'):
        policy.evaluate(str(tmp_path / project), level=1, justification='pequena', files=['models.py'])


def test_project_patterns_are_additive(tmp_path):
    folder = tmp_path / '.herdr'; folder.mkdir()
    (folder / 'review-gatilhos.json').write_text(json.dumps({'version': 1, 'gatilhos': {
        'pagamentos': ['billing/*'], 'segredos': ['tokens/*']}}))
    for path in ('billing/api.py', 'tokens/a', '.env'):
        with pytest.raises(RuntimeError, match='nível 2 obrigatório'):
            policy.evaluate(str(tmp_path), level=1, justification='teste', files=[path])


def test_unknown_configuration_fails_closed_only_for_level_one(tmp_path):
    (tmp_path / '.herdr').mkdir()
    (tmp_path / '.herdr/review-gatilhos.json').write_text('{broken')
    with pytest.raises(RuntimeError, match='configuração incerta'):
        policy.evaluate(str(tmp_path), level=1, justification='teste', files=['readme.txt'])
    assert policy.evaluate(str(tmp_path), files=['readme.txt'])['policy_error']


def test_declared_trigger_always_requires_two(tmp_path):
    with pytest.raises(RuntimeError, match='dados-pessoais'):
        policy.evaluate(str(tmp_path), level=1, justification='teste', declared=['dados-pessoais'], files=['readme'])


def test_git_inventory_includes_staged_deleted_untracked_and_rename_source(tmp_path):
    import subprocess
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@localhost')
    (tmp_path / '.env').write_text('placeholder')
    git('add', '.'); git('commit', '-m', 'fixture')
    git('mv', '.env', 'innocent.txt')
    (tmp_path / 'extra.txt').write_text('untracked')
    assert set(policy.changed_paths(str(tmp_path))) == {'.env', 'innocent.txt', 'extra.txt'}
    with pytest.raises(RuntimeError, match='segredos'):
        policy.evaluate(str(tmp_path), level=1, justification='rename')


def test_partial_never_returns_success():
    assert policy.outcome(['a', 'b'], ['a']) == ('parcial', 3)
    assert policy.outcome(['a', 'b'], []) == ('falha', 1)
    assert policy.outcome(['a', 'b'], ['a', 'b']) == ('completa', 0)
    assert policy.outcome(['a'], ['a']) == ('completa', 0)


def test_staged_secret_cannot_be_hidden_by_worktree(tmp_path):
    import subprocess
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@localhost')
    (tmp_path / 'readme').write_text('initial')
    git('add', '.'); git('commit', '-m', 'fixture')
    secret = tmp_path / '.env'; secret.write_text('placeholder'); git('add', '.env'); secret.unlink()
    assert '.env' in policy.changed_paths(str(tmp_path))
    with pytest.raises(RuntimeError, match='segredos'):
        policy.evaluate(str(tmp_path), level=1, justification='staged')
    secret.write_text('initial'); git('add', '.env'); git('commit', '-m', 'fixture two')
    secret.write_text('changed'); git('add', '.env'); secret.write_text('initial')
    assert '.env' in policy.changed_paths(str(tmp_path))
    with pytest.raises(RuntimeError, match='segredos'):
        policy.evaluate(str(tmp_path), level=1, justification='staged')


def test_symlink_and_relative_cwd_cannot_hide_private_project(tmp_path, monkeypatch):
    root = tmp_path / 'guardian'; root.mkdir()
    alias = tmp_path / 'innocent'; alias.symlink_to(root, target_is_directory=True)
    for cwd in (str(root), str(alias)):
        with pytest.raises(RuntimeError, match='privado'):
            policy.evaluate(cwd, level=1, justification='text', files=['readme'])
    monkeypatch.chdir(root)
    with pytest.raises(RuntimeError, match='privado'):
        policy.evaluate('.', level=1, justification='text', files=['readme'])


def test_subdirectory_reads_root_config_and_inventory(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    (tmp_path / 'sub').mkdir(); (tmp_path / '.env.local').write_text('placeholder')
    # Root canonicalization must find untracked root paths even when called
    # from a subdirectory.
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=T', '-c', 'user.email=t@localhost',
                    'commit', '--allow-empty', '-m', 'fixture'], check=True, capture_output=True)
    with pytest.raises(RuntimeError, match='segredos'):
        policy.evaluate(str(tmp_path / 'sub'), level=1, justification='text')
    (tmp_path / '.env.local').unlink()
    (tmp_path / '.herdr').mkdir()
    (tmp_path / '.herdr/review-gatilhos.json').write_text(json.dumps({
        'version': 1, 'gatilhos': {'billing': ['sub/billing.py']}}))
    with pytest.raises(RuntimeError, match='billing'):
        policy.evaluate(str(tmp_path / 'sub'), level=1, justification='text', files=['billing.py'])


def test_missing_git_is_uncertainty_not_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', '')
    assert policy.evaluate(str(tmp_path))['policy_error']
    with pytest.raises(RuntimeError, match='incerto'):
        policy.evaluate(str(tmp_path), level=1, justification='text')


def test_invalid_types_and_duplicate_expected(tmp_path):
    with pytest.raises(RuntimeError): policy.evaluate(str(tmp_path), level=True)
    with pytest.raises(RuntimeError): policy.evaluate(str(tmp_path), declared=[1])
    assert policy.outcome(['a', 'a'], ['a']) == ('completa', 0)


def test_gateway_project_intentionally_requires_level_two_for_all_paths(tmp_path):
    (tmp_path / 'llm-gateway').mkdir()
    with pytest.raises(RuntimeError, match='producao'):
        policy.evaluate(str(tmp_path / 'llm-gateway'), level=1, justification='text', files=['readme.txt'])


@pytest.mark.parametrize('factory', [iter, lambda xs: (x for x in xs)])
def test_declared_iterators_preserve_trigger(tmp_path, factory):
    with pytest.raises(RuntimeError, match='dados-pessoais'):
        policy.evaluate(str(tmp_path), level=1, justification='text',
                        declared=factory(['dados-pessoais']), files=['readme'])
    assert policy.evaluate(str(tmp_path), declared=factory(['dados-pessoais']),
                           files=['readme'])['gatilhos'][0]['nome'] == 'dados-pessoais'


def test_relative_files_and_file_symlinks_preserve_sensitive_directory(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    folder = tmp_path / 'deploy'; folder.mkdir(); (folder / 'release.sh').write_text('placeholder')
    assert policy.changed_paths(str(folder), files=['release.sh']) == ['deploy/release.sh']
    with pytest.raises(RuntimeError, match='producao'):
        policy.evaluate(str(folder), level=1, justification='text', files=['release.sh'])
    (tmp_path / 'alias').symlink_to(folder / 'release.sh')
    with pytest.raises(RuntimeError, match='producao'):
        policy.evaluate(str(tmp_path), level=1, justification='text', files=['alias'])
    (tmp_path / '.herdr').mkdir()
    (tmp_path / '.herdr/review-gatilhos.json').write_text(json.dumps({
        'version': 1, 'gatilhos': {'pagamentos': ['app/*']}}))
    app = tmp_path / 'app'; app.mkdir()
    with pytest.raises(RuntimeError, match='pagamentos'):
        policy.evaluate(str(app), level=1, justification='text', files=['x.py'])


def test_nonexistent_root_is_uncertainty(tmp_path):
    with pytest.raises(RuntimeError, match='diretório incerto'):
        policy.evaluate(str(tmp_path / 'missing'), level=1, justification='text', files=['readme'])
    assert policy.evaluate(str(tmp_path / 'missing'), files=['readme'])['policy_error']


@pytest.mark.parametrize('name,trigger', [('.env', 'segredos'), ('release.service', 'producao')])
def test_sensitive_link_name_and_resolved_target_are_both_checked(tmp_path, name, trigger):
    (tmp_path / 'config').mkdir(); (tmp_path / 'config/defaults.txt').write_text('placeholder')
    (tmp_path / name).symlink_to(tmp_path / 'config/defaults.txt')
    assert policy.changed_paths(str(tmp_path), files=[name]) == [name, 'config/defaults.txt']
    with pytest.raises(RuntimeError, match=trigger):
        policy.evaluate(str(tmp_path), level=1, justification='text', files=[name])


def test_explicit_invalid_git_directory_is_not_nonrepository(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'invalid-git-dir'))
    with pytest.raises(RuntimeError, match='raiz Git incerta'):
        policy.evaluate(str(tmp_path), level=1, justification='text', files=['readme'])
    assert policy.evaluate(str(tmp_path), files=['readme'])['policy_error']


def test_broken_git_marker_is_uncertainty(tmp_path):
    (tmp_path / '.git').mkdir()
    with pytest.raises(RuntimeError, match='raiz Git incerta'):
        policy.evaluate(str(tmp_path), level=1, justification='text', files=['readme'])


def test_dangling_policy_link_is_uncertainty(tmp_path):
    (tmp_path / '.herdr').mkdir()
    (tmp_path / '.herdr/review-gatilhos.json').symlink_to(tmp_path / 'missing-config')
    with pytest.raises(RuntimeError, match='configuração incerta'):
        policy.evaluate(str(tmp_path), level=1, justification='text', files=['readme'])
    result = policy.evaluate(str(tmp_path), files=['readme'])
    assert result['policy_error'] and result['gatilhos'][0]['nome'] == 'incerteza'


def test_dangling_policy_directory_is_uncertainty(tmp_path):
    (tmp_path / '.herdr').symlink_to(tmp_path / 'missing-folder', target_is_directory=True)
    with pytest.raises(RuntimeError, match='configuração incerta'):
        policy.evaluate(str(tmp_path), level=1, justification='text', files=['readme'])
