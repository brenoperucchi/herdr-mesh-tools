"""Evidence-based stderr advisories. Never changes guard decisions or starts background jobs."""
import os
from pathlib import Path
import re
import subprocess
import sys


def process(pid):
    base = Path('/proc') / str(pid)
    raw = (base / 'stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    argv = [os.fsdecode(x) for x in (base / 'cmdline').read_bytes().split(b'\0') if x]
    return dict(pid=pid, ppid=int(fields[1]), start=fields[19], argv=argv,
                executable=str((base / 'exe').resolve(strict=True)))


def opencode1(p):
    if not p['argv'] or Path(p['argv'][0]).name != 'opencode' or Path(p['executable']).name != 'opencode':
        return False
    result = subprocess.run([p['executable'], '--version'], capture_output=True, text=True,
                            timeout=1, stdin=subprocess.DEVNULL)
    # Only observed installed version 1.x, never infer from kind alone.
    return result.returncode == 0 and re.fullmatch(r'\s*(?:opencode\s+)?1\.\d+\.\d+(?:[-+][\w.]+)?\s*', result.stdout) is not None


def caller_opencode1():
    pid = os.getpid()
    for _ in range(32):
        p = process(pid)
        if opencode1(p):
            return p
        if p['ppid'] <= 1:
            return None
        pid = p['ppid']
    return None


def blocking_command(argv):
    if not argv:
        return None
    if Path(argv[0]).name == 'sleep' and len(argv) == 2:
        try:
            if float(argv[1]) >= 30:
                return 'sleep em primeiro plano de pelo menos 30 segundos'
        except ValueError:
            pass
    if Path(argv[0]).name == 'herdr' and '--wait' in argv and '--timeout' in argv:
        i = argv.index('--timeout')
        try:
            if int(argv[i + 1]) >= 30000:
                return 'herdr --wait de pelo menos 30 segundos'
        except (ValueError, IndexError):
            pass
    return None


def emit(operation, *, target=None, query=None):
    """Best effort; unavailable /proc/version evidence means no kind-specific claim."""
    try:
        if operation in ('herdr-ask', 'herdr-review-dispatch') and caller_opencode1():
            print(f'aviso: {operation} aguarda lifecycle e artefato. Caller OpenCode 1.x observado: '
                  'shell em primeiro plano pode impedir retorno de outro agent. Execute o dispatcher '
                  'em processo separado com log/resultado e devolva controle; não use sleep em foreground. '
                  'A revisão só conclui com artefato e exit code válidos.', file=sys.stderr)
        if target and target.get('agent') == 'opencode' and query:
            info = query('pane', 'process-info', '--pane', target['pane_id'])['process_info']
            procs = info.get('foreground_processes', [])
            verified = False
            for p in procs:
                observed = process(p['pid'])
                if opencode1(observed):
                    verified = True
                    break
            blocked = next((blocking_command(p.get('argv', [])) for p in procs
                            if blocking_command(p.get('argv', []))), None)
            if verified and blocked:
                print('aviso: alvo OpenCode 1.x observado com ' + blocked +
                      '; pode estar esperando retorno enquanto impede a entrega. Registre a resposta '
                      'na outbox e devolva controle. Este aviso não libera envio nem interrompe processos.',
                      file=sys.stderr)
    except Exception:  # advisory failure must never replace the original command result
        return
