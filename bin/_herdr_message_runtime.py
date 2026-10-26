"""Observed local caller and guarded CLI transport. No lifecycle wait, no input clearing."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import shlex
import tempfile
import subprocess
from contextlib import contextmanager

import _herdr_dispatch as core
from _herdr_message_ledger import MessageError, encode, endpoint, same_endpoint

CONTEXT_KEYS = {'HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH', 'HERDR_CONFIG_PATH',
                'HERDR_WORKSPACE_ID', 'HERDR_TAB_ID'}
SAFE_REFUSALS = {'empty_agent_prompt', 'agent_blocked', 'agent_not_ready', 'agent_not_found'}


def ancestors():
    """PID + start time, not names or a caller-supplied --from. Linux local only."""
    result = {}
    pid = os.getpid()
    for _ in range(64):
        try:
            raw = Path(f'/proc/{pid}/stat').read_text()
            fields = raw[raw.rfind(')') + 2:].split()
            parent, started = int(fields[1]), fields[19]
            result[pid] = started
        except (OSError, ValueError, IndexError) as exc:
            raise MessageError('identity_unproven: process ancestry unavailable') from exc
        if parent <= 1:
            return result
        pid = parent
    raise MessageError('identity_unproven: process ancestry too deep')


def local_context():
    """Recover routing hints hidden by Codex's shell sandbox, never caller authority."""
    env = dict(os.environ)
    for pid in ancestors():
        try:
            pairs = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        except OSError:
            continue
        for pair in pairs:
            key, sep, value = pair.partition(b'=')
            name = key.decode(errors='replace')
            if sep and name in CONTEXT_KEYS and name not in env:
                env[name] = value.decode(errors='strict')
    return env


@contextmanager
def file_lock(path, *, nonblocking=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise MessageError('unsafe lock file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError as exc:
            raise MessageError('reservation_busy') from exc
        yield
    finally:
        os.close(fd)


class Runtime:
    def __init__(self, env=None, route=None):
        self.env = local_context() if env is None else dict(env)
        if route is not None:
            if not isinstance(route, dict) or set(route) != {'socket', 'config', 'session'} or not all(isinstance(v, str) and '\x00' not in v for v in route.values()):
                raise MessageError('invalid_route')
            for key in ('socket', 'config'):
                if route[key] and not Path(route[key]).is_absolute():
                    raise MessageError('route_paths_must_be_absolute')
            for key, var in (('socket', 'HERDR_SOCKET_PATH'), ('config', 'HERDR_CONFIG_PATH'), ('session', 'HERDR_SESSION')):
                if route[key]:
                    self.env[var] = route[key]
                else:
                    self.env.pop(var, None)
        self.binary = str(Path(core.HERDR).resolve())
        self.session = self.env.get('HERDR_SESSION')
        # Same routing context used for every get, guard and prompt.
        route = dict(socket=self.env.get('HERDR_SOCKET_PATH', ''),
                     config=self.env.get('HERDR_CONFIG_PATH', ''), session=self.session or '')
        if not any(route.values()):
            raise MessageError('server_scope_unproven: explicit socket/config/session required')
        self.route = route
        self.scope = hashlib.sha256(encode(route).encode()).hexdigest()

    def argv(self, *args):
        return [self.binary, *(['--session', self.session] if self.session else []), *args]

    def read(self, *args):
        try:
            result = subprocess.run(self.argv(*args), env=self.env, capture_output=True,
                                    text=True, timeout=10)
            data = json.loads(result.stdout if result.returncode == 0 else (result.stderr or result.stdout))
            if not isinstance(data, dict):
                raise MessageError('herdr_read_failed')
            if result.returncode or 'error' in data:
                error = data.get('error')
                if isinstance(error, dict) and error.get('code') == 'agent_not_found':
                    raise MessageError('endpoint_missing')
                raise MessageError('herdr_read_failed')
            if not isinstance(data.get('result'), dict):
                raise MessageError('herdr_read_failed')
            return data['result']
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            raise MessageError('herdr_read_failed') from exc

    def get(self, target):
        info = self.read('agent', 'get', target).get('agent')
        if not isinstance(info, dict):
            raise MessageError('herdr_read_failed')
        endpoint(info)
        return info

    def caller(self):
        chain = ancestors()
        matches = []
        agents = self.read('agent', 'list').get('agents')
        if not isinstance(agents, list):
            raise MessageError('identity_unproven: invalid agent list')
        for info in agents:
            if not isinstance(info, dict) or not info.get('pane_id'):
                continue
            try:
                proc = self.read('pane', 'process-info', '--pane', info['pane_id']).get('process_info')
                if not isinstance(proc, dict) or not isinstance(proc.get('foreground_processes', []), list):
                    raise MessageError('identity_unproven: malformed process info')
                processes = proc.get('foreground_processes', [])
                if not all(isinstance(p, dict) and isinstance(p.get('pid'), int) for p in processes):
                    raise MessageError('identity_unproven: malformed foreground processes')
                foreground = {p['pid'] for p in processes}
                if proc.get('shell_pid') not in chain or not foreground.intersection(chain):
                    continue
                observed = self.get(info['pane_id'])
                if not same_endpoint(info, observed):
                    raise MessageError('identity_unproven: caller changed')
                matches.append(observed)
            except MessageError:
                # A failed lookup for an unrelated pane cannot grant caller identity.
                continue
        after = ancestors()
        if chain != after or len(matches) != 1:
            raise MessageError('identity_unproven: caller missing, changed or ambiguous')
        return endpoint(matches[0])

    def guard(self, info):
        guard = Path(__file__).resolve().with_name('herdr-can-send')
        try:
            result = subprocess.run([str(guard), info['pane_id'], '--json'], env=self.env,
                                    capture_output=True, text=True, timeout=35)
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                return False, 'guard_unreadable'
            if result.returncode or data.get('codigo') != 0 or data.get('pode_enviar') is not True:
                return False, 'origin_busy' if data.get('status') == 'working' else 'guard_refused'
            if data.get('pane') != info['pane_id'] or data.get('status') not in ('idle', 'done'):
                return False, 'guard_refused'
            return True, None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False, 'guard_unreadable'

    @contextmanager
    def reservation(self, info):
        # Same per-agent file as review/ask; do not wait holding a foreground shell.
        directory = Path(tempfile.gettempdir()) / 'herdr-dispatch-locks'
        directory.mkdir(mode=0o700, exist_ok=True)
        key = core._lock_component(info['workspace_id']) + '-' + core._lock_component(info['name'])
        with file_lock(directory / (key + '.lock')):
            yield

    def effective(self, info):
        def screen(observed):
            try:
                r = subprocess.run(self.argv('pane', 'read', observed['pane_id'], '--source', 'detection', '--lines', '15'),
                                   env=self.env, capture_output=True, text=True, timeout=10)
                return r.stdout if r.returncode == 0 else ''
            except (OSError, subprocess.TimeoutExpired):
                return ''
        return 'idle' if core.inferir_idle(info, screen) else info.get('agent_status')

    def command(self, directory, *args):
        return shlex.join([str(Path(__file__).resolve().with_name('herdr-message')),
                           '--route', encode(self.route), '--state-dir', str(Path(directory).absolute()), *args])

    def reply_command(self, directory, request_id):
        return self.command(directory, 'reply', request_id, '--key', 'reply:' + request_id)

    def status_command(self, directory, request_id):
        return self.command(directory, 'status', request_id)

    def prepare(self, expected):
        try:
            observed = self.get(expected['pane_id'])
        except MessageError as exc:
            if str(exc) == 'endpoint_missing':
                return False, 'endpoint_changed', None
            raise
        if not same_endpoint(expected, observed):
            return False, 'endpoint_changed', None
        # effective_status uses the existing bounded Codex inference, not unknown -> idle.
        if self.effective(observed) not in ('idle', 'done'):
            return False, 'origin_busy' if observed.get('agent_status') == 'working' else 'target_busy', observed
        allowed, reason = self.guard(observed)
        if not allowed:
            return False, reason, observed
        again = self.get(expected['pane_id'])
        fields = ('agent_status', 'state_change_seq', 'revision')
        if not same_endpoint(expected, again) or any(observed.get(k) != again.get(k) for k in fields):
            return False, 'preflight_changed', again
        return True, None, again

    def submit(self, expected, wire):
        # Only the supported agent surface; no --wait, --allow-working or raw terminal writes.
        try:
            result = subprocess.run(self.argv('agent', 'prompt', expected['pane_id'], wire),
                                    env=self.env, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return 'unknown', 'submission_result_unknown'
        try:
            data = json.loads(result.stdout if result.returncode == 0 else (result.stderr or result.stdout))
        except ValueError:
            return 'unknown', 'submission_result_unreadable'
        if not isinstance(data, dict):
            return 'unknown', 'receipt_mismatch'
        if result.returncode:
            error = data.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            return ('deferred', code) if isinstance(code, str) and code in SAFE_REFUSALS else ('unknown', 'submission_result_unknown')
        try:
            result_data = data.get('result')
            if not isinstance(result_data, dict) or not isinstance(result_data.get('agent'), dict):
                return 'unknown', 'receipt_mismatch'
            info = result_data['agent']
            if result_data['type'] != 'agent_prompted' or not same_endpoint(expected, info):
                raise MessageError('receipt mismatch')
        except (KeyError, TypeError, MessageError):
            return 'unknown', 'receipt_mismatch'
        return 'submitted', None


def envelope(message, reply_command=None):
    kind = message['kind']
    mid = message['id']
    sender = message['sender']['name']
    if kind == 'request' and message['requires_reply']:
        instruction = (f'Registre o retorno; escrever só no pane não registra resposta.\n'
                       f'Reply-Command: {reply_command} --file /caminho/absoluto\n'
                       
                       'O comando salva e devolve o controle; não use --wait ou sleep para aguardar a origem.')
    else:
        instruction = 'Mensagem sem obrigação de resposta; não gere cobrança nem novo pedido automaticamente.'
    return (f'[de {sender} para {message["destination"]["name"]}] '
            f'Assunto: {message["subject"]}\n'
            f'[herdr-message id={mid} tipo={kind} pedido={message["request_id"] or mid}]\n'
            f'{message["body"]}\n\n{instruction}')
