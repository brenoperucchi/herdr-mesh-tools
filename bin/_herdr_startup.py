"""Narrow startup UI handling; never a general dialog or composition bypass.

The menu predicate follows paperclip's current-worktree check, without a
connector dependency. A fresh physical pane receipt is required for Enter.
Identity/read/key remain external CLI operations with a residual UI race.
"""
from dataclasses import dataclass, field
import json
import os
import re
import subprocess
import time
import threading


class StartupDialogRefused(RuntimeError):
    pass


@dataclass
class NewPane:
    pane_id: str
    terminal_id: str
    cwd: str
    created_at: float
    consumed: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False, compare=False)


def created_pane_receipt(runtime, record, cwd):
    """Call ONLY on a successful pane/tab/workspace creation result.

    Existing free panes and restored names must never acquire this receipt.
    Missing identity evidence means no automatic UI confirmation.
    """
    pane = record.get('pane_id') if isinstance(record, dict) else None
    if not pane: return None
    try:
        observed = runtime.api('pane', 'get', pane)['pane']
    except (RuntimeError, KeyError, TypeError):
        return None
    if not isinstance(observed, dict) or observed.get('agent') or observed.get('pane_id') != pane or not observed.get('terminal_id'):
        return None
    return NewPane(pane, observed['terminal_id'], os.path.realpath(cwd), time.monotonic())


def read_detection(runtime, pane):
    result = subprocess.run(runtime.herdr_argv('agent', 'read', pane, '--source', 'detection', '--format', 'text'),
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise StartupDialogRefused(f'{pane}: não foi possível ler o diálogo de subida')
    return result.stdout


def _send_enter(runtime, pane):
    result = subprocess.run(runtime.herdr_argv('agent', 'send-keys', pane, 'enter'),
                            capture_output=True, text=True, timeout=10)
    return result.returncode == 0


def selected_current_worktree(screen):
    lines = screen.splitlines()
    title = [line for line in lines if line.strip(' \t│┃') == 'Where should the new conversation run?']
    selected = [line.strip(' \t│┃') for line in lines if re.match(r'^[ \t│┃]*›\s*\d+[.)]', line)]
    return (len(title) == 1 and len(selected) == 1 and re.fullmatch(
        r'›[ \t]+1\.[ \t]+Use current Git worktree(?:[ \t]+Keep using the current working directory)?',
        selected[0]) is not None)


def _identity(info):
    if not isinstance(info, dict):
        raise ValueError('metadado de agent inválido')
    return tuple(info.get(k) for k in ('name','agent','pane_id','terminal_id','cwd','foreground_cwd',
                                      'revision','state_change_seq','agent_status')) + (
        json.dumps(info.get('agent_session'),sort_keys=True),)


def confirm_new_conversation_dialog(runtime, receipt, *, name, kind, read=None, send_enter=None):
    if (kind != 'codex' or not isinstance(receipt, NewPane) or receipt.consumed
            or not receipt.terminal_id or not 0 <= time.monotonic() - receipt.created_at <= 180):
        return False
    read = read or (lambda pane: read_detection(runtime, pane))
    send_enter = send_enter or (lambda pane: _send_enter(runtime, pane))
    try:
        info = runtime.get_agent_info(receipt.pane_id)
        if not isinstance(info, dict): return False
        if any(type(info.get(k)) is not int or info[k] < 0 for k in ('revision','state_change_seq')):
            return False
        if (info.get('agent') != 'codex' or info.get('name') != name
                or info.get('pane_id') != receipt.pane_id or info.get('terminal_id') != receipt.terminal_id
                or info.get('agent_status') not in ('blocked','unknown','idle','done')
                or not info.get('cwd') or os.path.realpath(info['cwd']) != receipt.cwd
                or not info.get('foreground_cwd') or os.path.realpath(info['foreground_cwd']) != receipt.cwd):
            return False
        identity = _identity(info)
        if not selected_current_worktree(read(receipt.pane_id)):
            return False
        if _identity(runtime.get_agent_info(receipt.pane_id)) != identity:
            return False
        if not selected_current_worktree(read(receipt.pane_id)):
            return False
        if _identity(runtime.get_agent_info(receipt.pane_id)) != identity:
            return False
        # Record intent before the CLI call. Even an ambiguous failure must
        # never lead to a second Enter with the same startup capability.
        with receipt.lock:
            if receipt.consumed or not 0 <= time.monotonic() - receipt.created_at <= 180:
                return False
            receipt.consumed = True
        return bool(send_enter(receipt.pane_id))
    except (RuntimeError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        return False


def inspect_blocked_start(runtime, receipt, *, pane, name, kind, read=None, send_enter=None):
    read = read or (lambda target: read_detection(runtime, target))
    try:
        screen = read(pane)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise StartupDialogRefused(f'{pane}: leitura de diálogo ilegível; nenhuma tecla enviada') from exc
    if kind == 'claude' and ('Yes, I trust this folder' in screen or
                             'Do you trust' in screen or 'No, exit' in screen):
        raise StartupDialogRefused(f'{pane}: diálogo de confiança Claude requer intervenção; nenhuma tecla enviada.\n{dialog_excerpt(screen)}')
    if receipt is not None and receipt.pane_id != pane:
        return False
    if kind == 'codex' and selected_current_worktree(screen):
        # Re-read inside confirm: the initial screen is diagnostic, not an
        # authorization that can be reused after the selection changes.
        return confirm_new_conversation_dialog(runtime, receipt, name=name, kind=kind,
                                               read=read, send_enter=send_enter)
    return False


def dialog_excerpt(screen):
    markers = ('Do you trust', 'Yes, I trust this folder', 'No, exit',
               'Where should the new conversation run?', 'Use current Git worktree',
               'Create new Git worktree', 'Use session directory', 'Working directory',
               'press R to continue', 'Press R to continue')
    return '\n'.join(line[:160] for line in screen.splitlines()
                     if any(marker in line for marker in markers))[:480] or '(tela omitida)'


def blocked_diagnostic(runtime, info):
    pane = info.get('pane_id') or '(pane desconhecido)'
    try:
        screen = read_detection(runtime, pane)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        screen = '(leitura detection indisponível)'
    kind = info.get('agent') or 'agent'
    label = 'diálogo de confiança Claude' if kind == 'claude' and (
        'Yes, I trust this folder' in screen or 'No, exit' in screen or 'Do you trust' in screen) else f'{kind} bloqueado'
    # Shared metrics must not retain another task's terminal contents.
    excerpt = dialog_excerpt(screen)
    return f'{pane}: {label}; nenhuma tecla enviada.\n{excerpt or "(tela omitida)"}'


def finish_start(runtime, receipt, *, pane, name, kind, read=None, send_enter=None):
    """Inspect a start result before hydration, even if CLI reported ready.

    The worktree chooser may be detected as compose text (can-send=1), so its
    eligibility comes from a new-pane receipt and screen, never guard code=2.
    This function never causes /new and never sends any prompt text.
    """
    if kind not in ('codex', 'claude'):
        return False
    try:
        info = runtime.get_agent_info(pane)
        read = read or (lambda target:read_detection(runtime,target))
        screen = read(pane)
        if (not isinstance(info, dict) or info.get('pane_id') != pane
                or info.get('name') != name or info.get('agent') != kind):
            raise StartupDialogRefused(f'{pane}: identidade da subida não confirmada; nenhuma tecla enviada.\n{dialog_excerpt(screen)}')
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise StartupDialogRefused(f'{pane}: não foi possível atestar/ler a subida; nenhuma tecla enviada: {exc}') from exc
    if kind == 'claude':
        inspect_blocked_start(runtime, None, pane=pane, name=name, kind=kind,
                              read=lambda target:screen, send_enter=send_enter)
    if kind == 'codex' and any(marker in screen for marker in (
            'Use session directory', 'press R to continue', 'Press R to continue')):
        raise StartupDialogRefused(f'{pane}: diálogo legado Codex requer intervenção; nenhuma tecla enviada.\n{dialog_excerpt(screen)}')
    if kind == 'codex' and 'Where should the new conversation run?' in screen:
        if receipt is not None and receipt.pane_id != pane:
            raise StartupDialogRefused(f'{pane}: recibo de outro pane; nenhuma tecla enviada')
        if confirm_new_conversation_dialog(runtime,receipt,name=name,kind=kind,read=read,send_enter=send_enter):
            return True
        raise StartupDialogRefused(f'{pane}: diálogo Codex não elegível para confirmação automática; nenhuma tecla repetida/enviada.\n{dialog_excerpt(screen)}')
    if info.get('agent_status') == 'blocked':
        raise StartupDialogRefused(f'{pane}: {kind} bloqueado na subida; nenhuma tecla enviada.\n{dialog_excerpt(screen)}')
    return False
