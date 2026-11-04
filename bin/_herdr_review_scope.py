"""Registered repository targets; never a substitute for caller authentication."""
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess

import _herdr_review_groups as groups


def entries():
    # Trusted installed source, not a caller-selected project module.
    path = Path(__file__).resolve().with_name('herdr-bootstrap')
    loader = SourceFileLoader('_herdr_scope_bootstrap', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module.SPACES


def git_root(path):
    p = Path(path)
    if not p.is_absolute() or not p.is_dir():
        raise RuntimeError('repositório deve ser diretório absoluto existente')
    r = subprocess.run(['git', '-C', str(p.resolve()), 'rev-parse', '--show-toplevel'],
                       capture_output=True, text=True, timeout=10)
    if r.returncode:
        raise RuntimeError(f'diretório sem raiz Git: {p}')
    return str(Path(r.stdout.strip()).resolve())


def _repository(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise RuntimeError('repositório cadastrado deve ser absoluto')
    real = str(Path(path).resolve())
    if git_root(real) != real:
        raise RuntimeError(f'caminho deve ser a raiz Git: {path}')
    return real


def resolve(slug, requested, invoking, core):
    matches = [e for e in entries() if (e[4] if len(e)>4 else e[0].lower()) == slug]
    if len(matches) > 1:
        raise RuntimeError('slug duplicado em SPACES')
    if not matches:
        if requested is not None:
            raise RuntimeError('--repo exige space cadastrado em SPACES')
        return None
    e = matches[0]
    opts = e[5] if len(e)>5 else {}
    if not isinstance(opts, dict):
        raise RuntimeError('opções do space inválidas')
    additional = opts.get('additional_repositories', [])
    if requested is None and not additional:
        return None  # Preserve legacy no-Git/--files and group workflows.
    if not isinstance(additional, list) or not all(isinstance(p, str) for p in additional):
        raise RuntimeError('additional_repositories deve ser lista de caminhos absolutos')
    base = _repository(e[1])
    roots = [base, *[_repository(p) for p in additional]]
    if len(set(roots)) != len(roots):
        raise RuntimeError('repositórios duplicados no cadastro')
    target = _repository(requested) if requested is not None else git_root(invoking)
    if target not in roots or git_root(invoking) not in roots:
        raise RuntimeError('repositório alvo ou invocação fora do cadastro do space')
    owner = core.get_agent_info(e[2][0])
    if (owner.get('name') != e[2][0] or owner.get('agent') != e[2][1]
            or not isinstance(owner.get('agent_session'), dict)
            or not owner['agent_session'].get('value')
            or not all(owner.get(f) for f in ('pane_id','tab_id','workspace_id'))
            or any(not owner.get(f) or os.path.realpath(owner[f]) != base
                   for f in ('cwd','foreground_cwd'))):
        raise RuntimeError('coordenador não coincide com cwd/base/identidade cadastrados')
    return dict(slug=slug, base=base, target=target, repositories=roots,
                coordinator=groups.identity(owner))


def revalidate(scope, core):
    if scope is not None and resolve(scope['slug'], scope['target'], scope['base'], core) != scope:
        raise RuntimeError('vínculo de revisão mudou durante a operação')


def validate_files(target, files):
    root = Path(target).resolve()
    for file in files or []:
        try:
            Path(file).resolve(strict=True).relative_to(root)
        except (ValueError, OSError) as exc:
            raise RuntimeError('arquivo fora do repositório alvo') from exc


def validate_round(target, prior, slug):
    path = Path(prior).resolve()
    if (not path.is_dir() or path.parent != Path(target).resolve()/'.herdr'/'review'
            or not path.name.startswith(slug+'-')):
        raise RuntimeError('rodada anterior fora do repositório alvo/space')
    try:
        metrics = json.loads((path/'metrics.json').read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('rodada anterior sem métricas válidas') from exc
    if metrics.get('slug') != slug:
        raise RuntimeError('rodada anterior de outro space')
    recorded = metrics.get('review_scope')
    if recorded and recorded.get('target') != str(Path(target).resolve()):
        raise RuntimeError('raiz alvo da rodada anterior diverge')


@contextmanager
def reservation(scope, core):
    if scope is None:
        yield
        return
    directory = Path(os.environ.get('HERDR_MESH_STATE_DIR',
                                    os.path.expanduser('~/.local/state/herdr-mesh')))/'locks'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = json.dumps([core.herdr_argv(), scope['slug']], sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()
    fd = os.open(directory/f'review-space-{digest}.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
            raise RuntimeError('lock de revisão inseguro')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('outra rodada deste space em andamento') from exc
        revalidate(scope, core)
        yield
    finally:
        os.close(fd)


def freeze_files(files, round_dir, target):
    """Copy a checked open descriptor, never re-open an earlier path alias."""
    root=Path(target).resolve()
    mapping={}
    for original in files:
        resolved=Path(original).resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RuntimeError('fonte mudou para fora do alvo') from exc
        fd=os.open(resolved,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError('fonte não é arquivo regular')
            def contained():
                opened=Path(os.path.realpath(f'/proc/self/fd/{source.fileno()}'))
                try:
                    opened.relative_to(root)
                    if opened != Path(original).resolve(strict=True):
                        raise ValueError('alias mudou')
                except (ValueError,OSError) as exc:
                    raise RuntimeError('fonte aberta diverge do alvo validado') from exc
            contained()
            dest=Path(round_dir)/'_snapshot'/str(Path(original).absolute()).lstrip(os.sep)
            dest.parent.mkdir(parents=True,exist_ok=True)
            with dest.open('xb') as output:
                shutil.copyfileobj(source,output)
            after=os.fstat(source.fileno())
            contained()
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(
                    after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                raise RuntimeError('fonte mudou durante congelamento')
            mapping[original]=str(dest)
    return mapping
