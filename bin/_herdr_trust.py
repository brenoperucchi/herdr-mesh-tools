"""SPACES-scoped trust configuration. Audits never write; no CLI auto-installs.

JSON updates use optimistic before/after checks, at most two attempts. The
sidecar lock serializes mesh writers only; Claude does not honor that lock.
Atomic replacement guarantees a complete file, not isolation from a hostile
or noncooperating same-UID writer in the final check/replace window.
"""
import ast
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit
import uuid


class TrustError(RuntimeError):
    pass


class ConcurrentWrite(TrustError):
    pass


def validate_path_policy(directory, home):
    home = Path(home).resolve()
    lexical = Path(os.path.abspath(directory)); resolved = lexical.resolve()
    for candidate in (lexical, resolved):
        if candidate in (Path('/'), home, Path('/tmp'), Path('/var/tmp'), Path('/dev/shm')):
            raise TrustError('HOME, raiz e diretório temporário nunca recebem confiança automática')
        if any(part.casefold() == 'scratchpad' for part in candidate.parts):
            raise TrustError('scratchpad nunca recebe confiança automática')
        if any(candidate.is_relative_to(p) for p in (Path('/tmp'), Path('/var/tmp'), Path('/dev/shm'))) and not candidate.is_relative_to(home):
            raise TrustError('diretório temporário não recebe confiança automática')
    return resolved


def registered_directories(bootstrap, home, refused=None):
    try:
        tree = ast.parse(Path(bootstrap).read_text(encoding='utf-8'))
    except SyntaxError as exc:
        raise TrustError('tabela SPACES não pode ser analisada') from exc
    nodes = [n.value for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'SPACES' for t in n.targets)]
    if len(nodes) != 1 or not isinstance(nodes[0], ast.List):
        raise TrustError('tabela SPACES não reconhecida')
    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for item in node.values:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    parts.append(item.value)
                elif (isinstance(item, ast.FormattedValue) and isinstance(item.value, ast.Name)
                      and item.value.id == 'HOME' and item.conversion == -1 and item.format_spec is None):
                    parts.append(str(home))
                else:
                    raise TrustError('cwd dinâmico em SPACES')
            return ''.join(parts)
        # Only the default explicitly written in SPACES is authorized. An
        # environment override never gains trust just by changing the cwd.
        if (isinstance(node, ast.Call) and not node.keywords and len(node.args) == 2
                and isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
                and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == 'environ'
                and isinstance(node.func.value.value, ast.Name) and node.func.value.value.id == 'os'
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            if node.args[0].value in os.environ:
                raise TrustError('override de cwd definido; entrada não recebe confiança automática')
            return literal(node.args[1])
        raise TrustError('cwd dinâmico em SPACES')
    paths = set()
    for entry in nodes[0].elts:
        if not isinstance(entry, ast.Tuple) or len(entry.elts) < 2:
            raise TrustError('entrada SPACES inválida')
        try:
            value = literal(entry.elts[1])
            if not os.path.isabs(value): raise TrustError('cwd relativo em SPACES')
            paths.add(str(validate_path_policy(value, home)))
        except TrustError as exc:
            if refused is not None:
                label = entry.elts[0].value if isinstance(entry.elts[0], ast.Constant) else '(desconhecido)'
                refused.append({'label': str(label), 'directory': None, 'status': 'refused', 'reason': str(exc)})
    return paths


def codex_text(text, directory):
    """Edit a canonical project table without reserializing unrelated TOML.

    tomllib validates the full semantic result in render(). Unsupported TOML
    arrangements fail closed instead of introducing an interpreter dependency.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not line.lstrip().startswith('['): continue
        try: header = tomllib.loads(line)
        except ValueError: continue
        if (set(header) == {'projects'} and set(header['projects']) == {str(directory)}
                and header['projects'][str(directory)] == {}):
            if not lines[i].endswith('\n'): lines[i] += '\n'
            lines.insert(i + 1, 'trust_level = "trusted"\n')
            return ''.join(lines)
    ending = '\n' if not text or text.endswith('\n') else '\n\n'
    return text + ending + '[projects.' + json.dumps(str(directory), ensure_ascii=False) + ']\ntrust_level = "trusted"\n'


def git_root(directory):
    try:
        result = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True, timeout=5)
        return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def remote_warning(directory):
    result = subprocess.run(['git', '-C', str(directory), 'config', '--get', 'remote.origin.url'],
                            capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        return 'origem Git não verificável; confiar pode carregar .codex/ do projeto (hooks/MCP)'
    value = result.stdout.strip()
    if '://' in value:
        url = urlsplit(value); host = url.hostname or ''; owner = url.path.strip('/').split('/')[0]
    else:
        match = re.fullmatch(r'(?:[^@/:]+@)?([^/:]+):([^/]+)/.+', value)
        host, owner = match.groups() if match else ('', '')
    if host.casefold() != 'github.com' or owner.casefold() != 'brenoperucchi':
        return 'origin fora dos repositórios confirmados do Breno; confiar pode carregar .codex/ (hooks/MCP)'
    return None


def read_regular(path):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise TrustError(f'configuração deve ser arquivo regular sem links: {path}')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno()); content = stream.read(); after = os.fstat(stream.fileno())
    except Exception:
        raise
    fingerprint = lambda s: (s.st_ino, s.st_dev, s.st_mtime_ns, s.st_size)
    if fingerprint(before) != fingerprint(opened) or fingerprint(opened) != fingerprint(after):
        raise ConcurrentWrite('configuração mudou durante leitura')
    return content, stat.S_IMODE(before.st_mode), fingerprint(before)


def replace_if_unchanged(path, baseline, data):
    mode = baseline[1] if baseline is not None else 0o600
    fd, temporary = tempfile.mkstemp(prefix='.herdr-trust-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode); stream.write(data); stream.flush(); os.fsync(stream.fileno())
        if read_regular(path) != baseline:
            raise ConcurrentWrite('configuração mudou antes da escrita')
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


_BACKUPS = {}


def private_directory(home, area):
    root = Path(home) / '.local/state/herdr-trust'
    path = root / area
    if not path.resolve().is_relative_to(Path(home).resolve()):
        raise TrustError('estado de confiança escaparia da HOME; atualização recusada')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700); path.chmod(0o700)
    return path


@contextmanager
def writer_lock(path, home):
    key = hashlib.sha256(str(path).encode()).hexdigest()
    lock = private_directory(home, 'locks') / (key + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise ConcurrentWrite('outro helper de confiança está escrevendo') from exc
        yield
    finally:
        os.close(fd)


def backup(path, baseline, home):
    if baseline is None:
        return None
    key = hashlib.sha256(str(path).encode()).hexdigest()
    folder = private_directory(home, 'backups')
    cache_key = (str(path), str(folder))
    previous = _BACKUPS.get(cache_key)
    if previous and Path(previous).is_file() and not Path(previous).is_symlink():
        return previous
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    target = folder / f'{key}-{stamp}-{uuid.uuid4().hex[:8]}.bak'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(baseline[0]); stream.flush(); os.fsync(stream.fileno())
    _BACKUPS[cache_key] = str(target.resolve())
    # Rotate only this helper's regular backups of this exact config file.
    candidates = [p for p in folder.glob(key + '-*.bak') if p.is_file() and not p.is_symlink()]
    for old in sorted(candidates, key=lambda p:p.stat().st_mtime_ns, reverse=True)[10:]:
        old.unlink()
    return _BACKUPS[cache_key]


class TrustStore:
    def __init__(self, *, home=None, registered=None, bootstrap=None):
        self.home = Path(home or Path.home()).resolve()
        self.registry_refused = []
        if registered is not None:
            self.registered = set()
            for path in registered:
                try:
                    self.registered.add(str(validate_path_policy(path, self.home)))
                except (TrustError, OSError, RuntimeError) as exc:
                    self.registry_refused.append({'directory': str(path), 'status': 'refused', 'reason': str(exc)})
        else:
            self.registered = registered_directories(
                bootstrap or Path(__file__).resolve().with_name('herdr-bootstrap'), self.home, self.registry_refused)

    def config_path(self, kind):
        if kind == 'codex' and (self.home / '.codex').is_symlink():
            raise TrustError('diretório .codex é symlink; atualização automática recusada')
        return self.home / '.codex/config.toml' if kind == 'codex' else self.home / '.claude.json'

    def validate(self, directory):
        resolved = validate_path_policy(directory, self.home)
        if str(resolved) not in self.registered:
            raise TrustError('diretório fora de SPACES; confiança recusada')
        if not resolved.is_dir():
            raise TrustError('diretório registrado não existe')
        return resolved

    def decode(self, baseline, kind):
        text = baseline[0].decode('utf-8') if baseline else ''
        result = tomllib.loads(text) if kind == 'codex' else json.loads(text if baseline is not None else '{}')
        if not isinstance(result, dict) or not isinstance(result.get('projects', {}), dict):
            raise TrustError('estrutura projects inválida')
        return result

    def state(self, document, directory, kind):
        candidates = [directory]
        if kind == 'codex':
            root = git_root(directory)
            if root is not None: candidates.append(root)
        for candidate in candidates:
            entry = document.get('projects', {}).get(str(candidate), {})
            if not isinstance(entry, dict): raise TrustError('entrada de projeto inválida')
            if kind == 'codex':
                if entry.get('trust_level') == 'trusted': return 'already_trusted'
                if entry.get('trust_level') == 'untrusted': return 'explicitly_untrusted'
                if 'trust_level' in entry: raise TrustError('trust_level desconhecido; não sobrescrever')
            elif entry.get('hasTrustDialogAccepted') is True:
                return 'already_trusted'
        return 'missing'

    def trusted(self, document, directory, kind):
        return self.state(document, directory, kind) == 'already_trusted'

    def render(self, baseline, document, directory, kind):
        expected = copy.deepcopy(document)
        project = expected.setdefault('projects', {}).setdefault(str(directory), {})
        if not isinstance(project, dict): raise TrustError('entrada de projeto inválida')
        key, value = ('trust_level', 'trusted') if kind == 'codex' else ('hasTrustDialogAccepted', True)
        project[key] = value
        if kind == 'claude':
            text = json.dumps(expected, ensure_ascii=False, indent=2) + '\n'
            parsed = json.loads(text)
        else:
            text = codex_text(baseline[0].decode('utf-8') if baseline else '', directory)
            parsed = tomllib.loads(text)
        if parsed != expected:
            raise TrustError('edição alteraria outras chaves; nada escrito')
        return text.encode('utf-8')

    def ensure_trusted(self, directory, kind):
        result = {'directory': str(directory), 'kind': kind, 'status': 'warning', 'warnings': [], 'backup': None}
        if kind not in ('codex', 'claude'):
            result['status'] = 'not_applicable'; return result
        try:
            directory = self.validate(directory); result['directory'] = str(directory)
        except (TrustError, OSError, RuntimeError) as exc:
            result.update(status='refused', reason=str(exc)); return result
        try:
            try:
                warning = remote_warning(directory)
            except (OSError, ValueError, subprocess.SubprocessError):
                warning = 'origem Git não verificável; confiar pode carregar .codex/ (hooks/MCP)'
            if warning: result['warnings'].append(warning)
            path = self.config_path(kind)
            for attempt in range(2):
                try:
                    baseline = read_regular(path); document = self.decode(baseline, kind)
                    state = self.state(document, directory, kind)
                    if state in ('already_trusted', 'explicitly_untrusted'):
                        result['status'] = state
                        if state == 'explicitly_untrusted': result['reason'] = 'recusa explícita preservada; não sobrescrever'
                        return result
                    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    with writer_lock(path, self.home):
                        baseline = read_regular(path); document = self.decode(baseline, kind)
                        state = self.state(document, directory, kind)
                        if state in ('already_trusted', 'explicitly_untrusted'):
                            result['status'] = state
                            if state == 'explicitly_untrusted': result['reason'] = 'recusa explícita preservada; não sobrescrever'
                            return result
                        data = self.render(baseline, document, directory, kind)
                        result['backup'] = backup(path, baseline, self.home)
                        replace_if_unchanged(path, baseline, data)
                        after = self.decode(read_regular(path), kind)
                        if self.trusted(after, directory, kind):
                            result['status'] = 'trusted'; return result
                        raise ConcurrentWrite('CLI reescreveu a confiança após a escrita')
                except ConcurrentWrite:
                    if attempt: raise
                except ValueError:
                    if kind != 'claude' or attempt: raise
            raise TrustError('não foi possível confirmar confiança')
        except (TrustError, OSError, ValueError, ImportError, subprocess.SubprocessError) as exc:
            result['reason'] = str(exc); return result

    def audit(self):
        report = {'entries': [], 'outside': [], 'warnings': []}
        for entry in self.registry_refused:
            for kind in ('codex', 'claude'):
                report['entries'].append({**entry, 'kind': kind})
        for kind in ('codex', 'claude'):
            try: document = self.decode(read_regular(self.config_path(kind)), kind)
            except (TrustError, OSError, ValueError) as exc:
                report['warnings'].append(f'{kind}: {exc}'); document = None
            for directory in sorted(self.registered):
                entry = {'directory': directory, 'kind': kind}
                try:
                    path = self.validate(directory)
                    if document is None: raise TrustError('configuração não verificável')
                    entry['status'] = self.state(document, path, kind)
                except (TrustError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    entry.update(status='refused', reason=str(exc))
                report['entries'].append(entry)
            if document is not None:
                for directory, value in document.get('projects', {}).items():
                    if not os.path.isabs(directory) or os.path.realpath(directory) not in self.registered:
                        report['outside'].append({'kind': kind, 'directory': directory})
        return report


def ensure_trusted(directory, kind, *, store=None):
    try:
        result = (store or TrustStore()).ensure_trusted(directory, kind)
    except (OSError, ValueError, TrustError) as exc:
        result = {'directory': str(directory), 'kind': kind, 'status': 'warning', 'warnings': [], 'reason': str(exc)}
    for warning in result.get('warnings', []):
        print(f'herdr-trust: aviso: {directory}: {warning}', file=sys.stderr)
    if result['status'] in ('warning', 'refused', 'explicitly_untrusted'):
        print(f"herdr-trust: aviso: {directory}: {result.get('reason')}", file=sys.stderr)
    return result
