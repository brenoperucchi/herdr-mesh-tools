"""Read-only Codex thread binding and explicit resume ownership checks.

SQLite only opens a private copy: even mode=ro may create source WAL/SHM.
This is a precondition check, not a lease against concurrent external starts.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import shutil
import stat
import tempfile
import time

from _herdr_restart_argv import _option, VALUE_OPTIONS, SWITCHES, SELECTORS, COMMANDS

MAX_COPY_BYTES=512*1024*1024
COPY_PREFIX='herdr-restart-index-'
STALE_COPY_S=3600
PROC_ROOT=Path('/proc')


def _trusted_dir(p,t):
    me=os.getuid()
    if not stat.S_ISDIR(t.st_mode) or t.st_uid not in (0,me):raise Refused(f'ancestral do cache de outro dono: {p}')
    if t.st_mode&0o022 and not t.st_mode&stat.S_ISVTX:raise Refused(f'ancestral do cache gravável por grupo/outros: {p}')


def check_ancestors(path):
    """Recusa cadeia em que um terceiro possa trocar um componente por symlink depois da validacao (r208/r209).
    Resolve o caminho componente a componente, SEGUINDO cada symlink (relativos, encadeados, com limite de 8): todo
    diretorio fisico visitado precisa ter dono root ou nosso e nao ser gravavel por grupo/outros (exceto com sticky, como /tmp);
    todo symlink encontrado precisa ter dono root ou nosso. `..` e ciclos sao recusados (conservador)."""
    me=os.getuid();cur=Path('/');links=0
    try:_trusted_dir(cur,cur.stat())
    except OSError as exc:raise Refused(f'ancestral do cache ilegível: {cur}: {exc}') from exc
    pending=list(path.parts[1:])
    while pending:
        part=pending.pop(0)
        if part in ('','.'):continue
        if part=='..':raise Refused(f'caminho do cache com ..: {path}')
        nxt=cur/part
        try:
            l=nxt.lstat()
            if stat.S_ISLNK(l.st_mode):
                if l.st_uid not in (0,me):raise Refused(f'symlink de terceiro no caminho do cache: {nxt}')
                links+=1
                if links>8:raise Refused(f'symlinks demais no caminho do cache: {path}')
                target=os.readlink(nxt)
                if os.path.isabs(target):cur=Path('/')
                pending=[x for x in Path(target).parts if x!='/']+pending
                continue
            _trusted_dir(nxt,l)
        except OSError as exc:raise Refused(f'ancestral do cache ilegível: {nxt}: {exc}') from exc
        cur=nxt


def copy_base():
    """Diretorio das copias do indice: nunca o /tmp (tmpfs = RAM, ate 512 MB por chamada)."""
    root=Path(os.environ.get('HERDR_MESH_CACHE_DIR') or Path.home()/'.cache'/'herdr-mesh-tools')
    if not root.is_absolute():raise Refused('HERDR_MESH_CACHE_DIR deve ser absoluto')
    base=root/'tmp'
    try:
        base.mkdir(parents=True,exist_ok=True,mode=0o700)
        s=base.lstat()
    except OSError as exc:raise Refused(f'diretório de cópias indisponível: {exc}') from exc
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=os.getuid():raise Refused('diretório de cópias inseguro')
    if s.st_mode&0o022:raise Refused('diretório de cópias gravável por grupo/outros; recuse em vez de corrigir em silêncio')
    check_ancestors(base)
    return base


def hold_copy(path):
    """Reserva de uso da copia: flock exclusivo no proprio diretorio, solto pelo kernel se o processo morrer."""
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        os.close(fd);raise
    return fd


def sweep_stale(base):
    """Remove sobras de chamadas mortas (SIGKILL/queda de energia): so diretorios proprios com o prefixo, nao-symlink,
    parados ha mais de 1 h E cuja reserva de uso (flock) esteja livre. Uma chamada viva, mesmo pausada por horas,
    segura a reserva e nunca e' apagada (a idade sozinha nao prova que a chamada morreu)."""
    try:entries=list(base.iterdir())
    except OSError:return
    for e in entries:
        fd=None
        try:
            s=e.lstat()
            if not (e.name.startswith(COPY_PREFIX) and stat.S_ISDIR(s.st_mode) and s.st_uid==os.getuid()
                    and time.time()-s.st_mtime>STALE_COPY_S):continue
            fd=hold_copy(e)             # ocupada (BlockingIOError) = chamada viva: pula
            shutil.rmtree(e,ignore_errors=True)
        except OSError:continue
        finally:
            if fd is not None:os.close(fd)


class Refused(ValueError):
    pass


def index_path():
    root=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    if not root.is_absolute():raise Refused('CODEX_HOME deve ser absoluto')
    return root.resolve()/'state_5.sqlite'


def signature(path,optional=False):
    try:s=path.lstat()
    except FileNotFoundError:
        if optional:return None
        raise Refused(f'índice da sessão não existe: {path}')
    if (not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1
            or s.st_size>MAX_COPY_BYTES):
        raise Refused(f'fonte SQLite insegura ou muito grande: {path}')
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)


@contextmanager
def thread_index():
    source=index_path();wal=Path(str(source)+'-wal');journal=Path(str(source)+'-journal')
    last=None
    base=copy_base();sweep_stale(base)
    for _ in range(2):
        tmp=tempfile.TemporaryDirectory(prefix=COPY_PREFIX,dir=base)
        db=None;lock=None
        try:
            lock=hold_copy(tmp.name)    # reserva de uso por TODO o contexto (copia + consumidor)
            before=(signature(source),signature(wal,True),signature(journal,True))
            if before[2] is not None:raise Refused('journal SQLite presente; não é seguro escolher sessão')
            if sum(x[2] for x in before if x)>MAX_COPY_BYTES:raise Refused('snapshot SQLite muito grande')
            target=Path(tmp.name)/source.name
            for original,dest,expected in ((source,target,before[0]),(wal,Path(str(target)+'-wal'),before[1])):
                if expected is None:continue
                fd=os.open(original,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as inp,dest.open('xb') as out:
                    s=os.fstat(inp.fileno())
                    if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)!=expected:
                        raise Refused('índice mudou durante leitura')
                    remaining=expected[2]
                    while remaining:
                        chunk=inp.read(min(1024*1024,remaining))
                        if not chunk:raise Refused('índice truncado durante leitura')
                        out.write(chunk);remaining-=len(chunk)
            after=(signature(source),signature(wal,True),signature(journal,True))
            if before!=after:raise Refused('índice mudou durante snapshot')
            # Only the disposable copy is opened by SQLite. Do not copy SHM.
            db=sqlite3.connect(str(target))
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            cols={r[1] for r in db.execute('PRAGMA table_info(threads)')}
            if not {'id','cwd'}<=cols:raise Refused('schema threads sem id/cwd')
        except (OSError,sqlite3.Error,Refused) as exc:
            if db is not None:db.close()
            tmp.cleanup();last=exc
            if lock is not None:os.close(lock)
            continue
        except BaseException:
            # qualquer outra falha na copia tambem apaga a copia (a limpeza vale em todo caminho)
            if db is not None:db.close()
            tmp.cleanup()
            if lock is not None:os.close(lock)
            raise
        # Do not catch a consumer's validation error as a copy retry: a
        # contextmanager must yield exactly once, including failure paths.
        try:
            yield db,cols
        except sqlite3.Error as exc:
            raise Refused(f'consulta de sessão recusada: {exc}') from exc
        finally:
            db.close();tmp.cleanup();os.close(lock)
        return
    raise Refused(f'não foi possível ler índice de sessão: {last}')


def canonical_cwd(value):
    if not isinstance(value,str) or not value or '\0' in value or not os.path.isabs(value):
        raise Refused('cwd de sessão/pane ausente ou não absoluto')
    return os.path.realpath(value)


def pane_cwd(info):
    cwd=canonical_cwd(info.get('cwd'))
    if info.get('foreground_cwd') and canonical_cwd(info['foreground_cwd'])!=cwd:
        raise Refused(f"pane {info['pane_id']}: cwd e foreground_cwd divergem")
    return cwd


def thread(session):
    if not isinstance(session,str) or not session or '\0' in session:raise Refused('sessão inválida')
    with thread_index() as (db,_):
        rows=db.execute('SELECT cwd FROM threads WHERE id=? LIMIT 2',(session,)).fetchall()
        if len(rows)!=1:raise Refused(f'sessão {session} ausente ou ambígua no índice')
        return canonical_cwd(rows[0][0])


def check_neighbors(info,own_pids,table):
    """Recusa quando outro Codex vivo no mesmo cwd tem sessao que nao se prova por argv `resume` (ou argv
    que nao se consegue interpretar). So o registro nao basta: agent sem sessao registrada nao aparece nele."""
    cwd=pane_cwd(info)
    for pid,item in (table or {}).items():
        if pid in own_pids or item.get('state')=='Z' or not item.get('argv'):continue
        if os.path.basename(item['argv'][0]) not in ('codex','codex.exe'):continue
        try:same=canonical_cwd(item.get('cwd'))==cwd
        except Refused:continue
        if not same:continue
        try:chosen=resume_id(item['argv'])
        except Refused as exc:
            raise Refused(f'há outro Codex vivo neste cwd (PID {pid}) com argv não interpretável ({exc}); '
                          'informe --session a partir do registro do próprio agent') from exc
        if chosen is None:
            raise Refused(f'há outro Codex vivo neste cwd (PID {pid}) sem sessão comprovável por resume; '
                          'informe --session a partir do registro do próprio agent')


def latest(info,*,exclude=(),own_pids=frozenset(),table=None):
    """Sessao sugerida por cwd exato. Nunca sugere uma sessao declarada por outro agent ou retomada por outro
    processo (`exclude`), e recusa sugerir quando ha outro Codex vivo no mesmo cwd sem sessao comprovavel."""
    cwd=pane_cwd(info)
    check_neighbors(info,own_pids,table)
    skip=set(exclude)
    with thread_index() as (db,cols):
        if 'updated_at_ms' not in cols:raise Refused('índice sem updated_at_ms; informe --session explicitamente')
        def realpath(value):
            try:return canonical_cwd(value)
            except Refused:return None
        db.create_function('mesh_cwd',1,realpath,deterministic=True)
        for sid,tcwd in db.execute('SELECT id,cwd FROM threads WHERE mesh_cwd(cwd)=? '
                                   'ORDER BY updated_at_ms DESC,id DESC',(cwd,)):
            if sid in skip:continue
            return {'session':sid,'cwd':canonical_cwd(tcwd)}
    return None


def pane_pids(table,process_info):
    """PIDs que pertencem ao pane (ancestria ate o shell); vazio se o pane nao for observavel."""
    try:
        shell=process_info['shell_pid'];fg={p['pid'] for p in process_info['foreground_processes']}
        return {pid for pid in table if inside(pid,shell,fg,table)}
    except (KeyError,TypeError,Refused):
        return set()


def sessions_in_use(info,agents,table,own_pids=frozenset()):
    """Sessoes que NAO podem ser sugeridas: declaradas por outro agent ou retomadas por qualquer processo."""
    used=set()
    for agent in agents or []:
        sid=agent.get('agent_session') if isinstance(agent,dict) else None
        if isinstance(sid,dict) and sid.get('value') and agent.get('pane_id')!=info.get('pane_id'):
            used.add(sid['value'])
    for pid,item in (table or {}).items():
        if pid in own_pids:continue  # processos do proprio pane nao contam como "outros"
        try:chosen=resume_id(item.get('argv') or [])
        except Refused as exc:raise Refused(f'PID {pid}: argv Codex não interpretável ({exc}); informe --session') from exc
        if chosen:used.add(chosen)
    return used


def resume_id(argv):
    """Id do `codex resume <id>` explicito, None se comprovadamente nao ha resume. Qualquer forma que o parser
    nao consiga fechar (posicional antes do subcomando seguido de `resume`, `resume` sem id) recusa: nunca
    devolve None quando um resume pode existir (achados r200)."""
    if not argv or os.path.basename(argv[0]) not in ('codex','codex.exe'):return None
    seen=False;i=1;positional=False
    while i<len(argv):
        token=argv[i]
        if token=='--':
            if seen:raise Refused('resume sem id explícito antes de --')
            return None
        if token=='resume' and not seen:
            if positional:raise Refused('argv Codex ambíguo: resume depois de um argumento posicional')
            seen=True;i+=1;continue
        key,attached=_option(token)
        if key in VALUE_OPTIONS:
            if attached is None and i+1>=len(argv):raise Refused('argv Codex incompleto')
            i+=1 if attached is not None else 2;continue
        if key in ('-i','--image'):
            # variadico (ver _herdr_restart_argv): consome ate a proxima opcao; `resume` no meio e' ambiguo
            if attached is not None:i+=1
            else:
                i+=1
                while i<len(argv) and not argv[i].startswith('-'):
                    if argv[i]=='resume':raise Refused('argv Codex ambíguo: resume depois de --image')
                    i+=1
            continue
        if key in SWITCHES or key in SELECTORS:i+=1;continue
        if token.startswith('-'):raise Refused('opção Codex desconhecida ao procurar resume')
        if seen:return token
        if token in ('exec','e') and 'resume' in argv[i+1:]:
            raise Refused('exec resume não verificável: `codex exec resume` também retoma sessão')
        if token in COMMANDS:return None   # review/app-server/fork...: subcomando que nao retoma a sessao de origem
        positional=True;i+=1   # prompt/posicional do nivel principal: segue procurando por `resume`
    if seen:raise Refused('resume sem id explícito')
    return None


def process_table():
    result={}
    for path in PROC_ROOT.iterdir():
        if not path.name.isdigit():continue
        try:
            if path.stat().st_uid!=os.getuid():continue
            raw=(path/'stat').read_text();fields=raw[raw.rfind(')')+2:].split()
            name=raw[raw.find('(')+1:raw.rfind(')')]
            argv=([v.decode('utf-8') for v in (path/'cmdline').read_bytes().split(b'\0') if v]
                  if name in ('codex','codex.exe') else [])
            result[int(path.name)]=dict(pid=int(path.name),ppid=int(fields[1]),start=fields[19],
                                       name=name,state=fields[0],argv=argv,
                                       cwd=os.readlink(path/'cwd') if argv else None)
        except (FileNotFoundError,ProcessLookupError):continue
        except (OSError,ValueError,IndexError,UnicodeError) as exc:
            raise Refused(f'não foi possível observar processo PID {path.name}') from exc
    return result


def inside(pid,shell,foreground,table):
    chain=set();current=pid
    for _ in range(128):
        if current in chain:raise Refused('ciclo na ancestralidade do processo')
        chain.add(current)
        item=table.get(current)
        if not item:break
        current=item['ppid']
        if current<=1:break
    return shell in chain and bool(chain & foreground)


def validate(info,session,old_pid,process_info):
    expected=pane_cwd(info);observed=thread(session);pane=info['pane_id']
    if observed!=expected:
        raise Refused(f'pane={pane} sessão={session}: cwd_da_sessão={observed} cwd_do_pane={expected}')
    if not isinstance(process_info,dict):raise Refused('metadata de processo do pane ausente')
    if process_info.get('pane_id')!=pane:raise Refused('processos pertencem a outro pane')
    shell=process_info.get('shell_pid');foreground=process_info.get('foreground_processes')
    if type(shell) is not int or not isinstance(foreground,list):raise Refused('ownership do pane não observável')
    if any(not isinstance(p,dict) or type(p.get('pid')) is not int for p in foreground):
        raise Refused('processos de foreground inválidos')
    foreground={p['pid'] for p in foreground}
    table=process_table()
    if shell not in table:raise Refused('shell do pane não observável')
    if old_pid is not None and (old_pid not in table or not inside(old_pid,shell,foreground,table)):
        raise Refused(f'PID {old_pid} não pertence ao pane {pane}; nada será encerrado')
    if old_pid is not None:
        native=table[old_pid]
        if (native.get('name') not in ('codex','codex.exe') or not native.get('argv')
                or os.path.basename(native['argv'][0]) not in ('codex','codex.exe')
                or canonical_cwd(native.get('cwd'))!=expected):
            raise Refused(f'PID {old_pid}: kind/cwd do processo não coincide com o pane {pane}')
    if old_pid is not None:
        try:live=resume_id(table[old_pid]['argv'])
        except Refused as exc:raise Refused(f'PID {old_pid}: resume do processo alvo não verificável ({exc}); nada será encerrado') from exc
        if live is not None and live!=session:
            raise Refused(f'PID {old_pid}: o processo vivo retoma {live}, mas a sessão escolhida é {session}; nada será encerrado')
    for pid,item in table.items():
        if item['state']=='Z':continue
        try:chosen=resume_id(item['argv'])
        except Refused:
            # `resume --last`/`exec resume --last` nao trazem o id no argv: fora do pane e no mesmo cwd da sessao
            # contam como possivel segundo escritor (achado r202/rev-2)
            try:same=canonical_cwd(item.get('cwd'))==observed
            except Refused:same=False
            if session in item['argv'] or (same and not inside(pid,shell,foreground,table)):
                raise Refused(f'PID {pid}: resume não verificável no mesmo cwd da sessão {session}')
            continue
        if chosen==session and not inside(pid,shell,foreground,table):
            raise Refused(f'pane={pane} sessão={session}: outro codex resume usa a sessão fora do pane, PID {pid}')
    binding={field:info.get(field) for field in ('pane_id','terminal_id','workspace_id','tab_id')}
    if not all(isinstance(v,str) and v for v in binding.values()):
        raise Refused('identidade física do pane incompleta antes do restart')
    return {'session':session,'cwd':observed,
            'pid_start':table[old_pid]['start'] if old_pid is not None else None,
            'shell_pid':shell,'shell_start':table[shell]['start'],'binding':binding}


def unique_session(info,session,agents):
    if not isinstance(agents,list) or any(not isinstance(a,dict) for a in agents):
        raise Refused('registro de agents não observável')
    for agent in agents:
        sid=agent.get('agent_session')
        if (isinstance(sid,dict) and sid.get('value')==session
                and agent.get('pane_id')!=info['pane_id']):
            raise Refused(f"sessão declarada por outro agent: {agent.get('name')} pane={agent.get('pane_id')}")


def after_stop(plan,pane,process_info):
    """Validate physical continuity without requiring a departed agent record.

    The session/cwd decision is frozen before termination. Only native process
    ownership and explicit duplicate resumes are reobserved here.
    """
    if not isinstance(pane,dict) or not isinstance(process_info,dict):
        raise Refused('pane/processos indisponíveis depois de encerrar')
    for field,value in plan['binding'].items():
        if pane.get(field)!=value:raise Refused(f'identidade física do pane mudou: {field}')
    if process_info.get('pane_id')!=plan['binding']['pane_id']:
        raise Refused('processos pertencem a outro pane')
    if pane_cwd(pane)!=plan['cwd']:raise Refused('cwd do pane mudou depois de encerrar')
    if pane.get('agent') not in (None,'unknown') or pane.get('agent_session'):
        raise Refused('pane já está ocupado por um agent')
    shell=process_info.get('shell_pid');foreground=process_info.get('foreground_processes')
    if shell!=plan['shell_pid'] or not isinstance(foreground,list):
        raise Refused('shell do pane mudou ou não é observável')
    if (not foreground or any(not isinstance(p,dict) or type(p.get('pid')) is not int for p in foreground)
            or {p['pid'] for p in foreground}!={shell}):
        raise Refused('pane não voltou ao shell em foreground')
    if any(canonical_cwd(p.get('cwd'))!=plan['cwd'] for p in foreground):
        raise Refused('cwd do shell não coincide com o plano')
    table=process_table();native=table.get(shell)
    if (not native or native['start']!=plan['shell_start'] or native['state']=='Z'
            or native['name'] not in ('bash','zsh','fish','sh','dash','ksh','nu')):
        raise Refused('processo shell original não foi confirmado')
    for pid,item in table.items():
        if item['state']=='Z':continue
        try:chosen=resume_id(item['argv'])
        except Refused:
            try:same=canonical_cwd(item.get('cwd'))==plan['cwd']
            except Refused:same=False
            if plan['session'] in item['argv'] or same:   # o processo antigo ja saiu: qualquer um no mesmo cwd conta
                raise Refused(f'PID {pid}: resume não verificável no mesmo cwd da sessão')
            continue
        if chosen==plan['session']:
            raise Refused(f'outro codex resume usa a sessão após encerramento, PID {pid}')
