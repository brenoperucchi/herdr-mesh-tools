"""Single first-turn Codex handshake. Normal delivery remains session-strict."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import _herdr_review_groups as groups
from _herdr_restart_argv import VALUE_OPTIONS, SWITCHES, _option

TIMEOUT_S = 120


def _initial_argv(argv):
    if not argv or os.path.basename(argv[0]) != 'codex':
        raise RuntimeError('foreground não é um lançamento Codex direto')
    i = 1
    while i < len(argv):
        key, attached = _option(argv[i])
        if key in SWITCHES and attached is None:
            i += 1
        elif key in VALUE_OPTIONS:
            if attached is None:
                i += 1
                if i >= len(argv) or not argv[i]:
                    raise RuntimeError('argv Codex incompleto')
            elif not attached:
                raise RuntimeError('argv Codex incompleto')
            i += 1
        else:
            raise RuntimeError('hidratação inicial recusa resume, prompt posicional e opção desconhecida')


def _proc_identity(pid):
    """Read exact live process identity; no session inference or environment dump."""
    path = Path('/proc') / str(pid)
    stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    argv = (path / 'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    return dict(starttime=stat[19], state=stat[0], argv=argv,
                cwd=os.path.realpath(path / 'cwd'), executable=os.path.realpath(path / 'exe'),
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def process_identity(info, core, root):
    data = core.api('pane', 'process-info', '--pane', info['pane_id'])['process_info']
    if data.get('pane_id') != info['pane_id']:
        raise RuntimeError('process-info resolveu outro pane')
    candidates = [p for p in data.get('foreground_processes', [])
                  if p.get('name') == 'codex' or (p.get('argv') and os.path.basename(p['argv'][0]) == 'codex')]
    if len(candidates) != 1:
        raise RuntimeError('foreground Codex ausente ou ambíguo')
    process = candidates[0]
    pid = process.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError('PID Codex inválido')
    native = _proc_identity(pid)
    _initial_argv(native['argv'])
    if (native['state'] in ('T', 't', 'Z', 'X') or native['argv'] != process.get('argv')
            or os.path.basename(native['executable']) != 'codex'
            or core.project_root(native['cwd']) != root
            or core.project_root(process.get('cwd', '')) != root):
        raise RuntimeError('processo Codex não coincide com o foreground/raiz')
    return {k: native[k] for k in ('starttime', 'boot_id', 'executable')} | {
        'pid': pid, 'argv_sha256': hashlib.sha256(json.dumps(native['argv']).encode()).hexdigest()}


def _receipt_path(binding, name, info, process, core):
    state = Path(os.environ.get('HERDR_MESH_STATE_DIR') or Path.home() / '.local/state/herdr-mesh')
    key = [core.herdr_argv(), binding['slug'], binding['root'], name, info['terminal_id'],
           {k: process[k] for k in ('pid', 'starttime', 'boot_id')}]
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return state / 'hydration' / (digest + '.json')


def _persist(path, receipt, core):
    core.write_json_atomic(str(path), receipt)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _same_process(binding, name, before, process, core, *, initial):
    groups.revalidate(binding, core)
    observed = core.get_agent_info(name)
    if initial:
        groups.validate_initial_codex_member(binding, name, observed, core)
    else:
        groups._check_location(observed, binding['root'], core)
        if observed.get('name') != name:
            raise RuntimeError('nome mudou durante hidratação')
        groups._check_member_topology(binding, name, observed)
    a, b = groups.identity(before), groups.identity(observed)
    a.pop('agent_session'); b.pop('agent_session')
    if a != b or process_identity(observed, core, binding['root']) != process:
        raise RuntimeError('identidade de pane/processo mudou durante hidratação')
    return observed


def _native_session(info):
    session = info.get('agent_session')
    return (isinstance(session, dict) and session.get('value')
            and session.get('source') == 'herdr:codex' and session.get('kind') == 'id')


def hydrate_locked(binding, name, pane, core, *, dry_run=False):
    """Caller holds group then target locks. Accepts no arbitrary prompt."""
    groups.revalidate(binding, core)
    info = core.get_agent_info(name)
    groups.validate_initial_codex_member(binding, name, info, core)
    if info['pane_id'] != pane:
        raise RuntimeError('pane inicial diverge do alvo explicitamente autorizado')
    group = groups.get_group(binding['slug'])
    # Only the selected role is exempt from the normal native-session check.
    for other in [f'{group.slug}-rev', *group.roles()]:
        if other == name:
            continue
        try:
            member = core.get_agent_info(other)
        except RuntimeError as exc:
            if core._is_agent_not_found(exc):
                continue
            raise
        groups.validate_member(binding, other, member, core)
    process = process_identity(info, core, binding['root'])
    path = _receipt_path(binding, name, info, process, core)
    if path.exists():
        raise RuntimeError(f'recibo inicial existente; não reenviar: {path}')
    _same_process(binding, name, info, process, core, initial=True)
    core.require_can_send(name, expected_pane=pane)
    _same_process(binding, name, info, process, core, initial=True)
    print(f'hidratação inicial {name}: pane={pane} terminal={info["terminal_id"]} '
          f'pid={process["pid"]} starttime={process["starttime"]}; can-send=0')
    if dry_run:
        print('dry-run: sem entrega, sem recibo, sem criação/restart')
        return info
    prompt = core.role_reinforcement_prompt(name, group.slug, binding['root'],
        [group.coordinator_name, *[n for n in group.roles() if n != name]],
        coordinator_name=group.coordinator_name)
    receipt = dict(version=1, operation_id=str(uuid.uuid4()), state='prepared',
                   created_at=core.now_iso(), binding=binding, target=groups.identity(info),
                   process=process, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    _persist(path, receipt, core)
    receipt['state'] = 'delivery_attempted'
    _persist(path, receipt, core)
    submitted = False
    try:
        # After durable I/O, fence again immediately around the final guard.
        _same_process(binding, name, info, process, core, initial=True)
        core.require_can_send(name, expected_pane=pane)
        _same_process(binding, name, info, process, core, initial=True)
        submitted = True
        try:
            result = subprocess.run(core.herdr_argv('agent', 'prompt', pane, prompt, '--wait',
                                    '--until', 'working', '--timeout', '30000'),
                                    capture_output=True, text=True, timeout=core.CLI_TIMEOUT_S)
            if result.returncode:
                try:
                    error_code = json.loads(result.stdout or result.stderr)['error']['code']
                except (TypeError, ValueError, KeyError):
                    error_code = f'exit_{result.returncode}'
                receipt.update(state='delivery_unconfirmed', delivery_error=error_code)
                _persist(path, receipt, core)
        except (OSError, subprocess.TimeoutExpired) as exc:
            receipt.update(state='delivery_unconfirmed', delivery_error=type(exc).__name__)
            _persist(path, receipt, core)
        # A stalled/uncertain transport may already have delivered. Observe only;
        # neither a nonzero return nor a timeout authorizes another submission.
        deadline = time.monotonic() + TIMEOUT_S
        while True:
            current = _same_process(binding, name, info, process, core, initial=False)
            if (_native_session(current) and core.effective_agent_status(name) in ('idle', 'done')
                    and current.get('interactive_ready') is True):
                groups.validate_member(binding, name, current, core)
                receipt.update(state='complete', completed_at=core.now_iso(),
                               native_session=current['agent_session'])
                _persist(path, receipt, core)
                print(f'{name}: hydrated=true; sessão nativa comprovada; recibo={path}')
                return current
            if current.get('agent_status') == 'blocked' or time.monotonic() >= deadline:
                raise RuntimeError('primeiro turno sem sessão nativa assentada; não reenviar')
            time.sleep(0.5)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        receipt.update(state='ambiguous' if submitted else 'aborted_before_send',
                       error=str(exc), observed_at=core.now_iso())
        _persist(path, receipt, core)
        raise RuntimeError(f'hidratação inicial incompleta; pane preservado; recibo={path}: {exc}') from exc
