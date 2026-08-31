"""Mecânica de migração de nome do primeiro revisor de cada space, de
`<slug>-rev` para `<slug>-rev-1` (`<slug>-rev-2` nunca muda de nome).

Isto é o mecanismo, não a decisão de quando migrar: `herdr-migrate-rev`
(script separado) é o driver que usa estas funções para migrar um space por
vez. `herdr-review-dispatch`, `herdr-ask` e `herdr-bootstrap` só *leem* o
estado escrito aqui — nenhum dispatcher escreve em `migration-state.json`.

Histórico: cinco rodadas de revisão (`herdr-4` a `herdr-8` em
`.herdr/review/`) reprovaram versões anteriores deste mecanismo — as três
primeiras por falta de lock atômico de verdade, identidade de escrita
divergindo da attestation, e `_infer_role` sem `-rev-1`; a quarta (`herdr-7`,
primeira revisão de código real) por lock baseado em existência de arquivo
com corrida real, `pending-manual` não respeitado, `round_in_flight` sem
janela de tempo, e resultado de `agent rename` tratado como certo sem
consultar o que aconteceu de fato; a quinta (`herdr-8`) por dois efeitos
colaterais das próprias correções da `herdr-7`: `space_is_gated()` dizia
cobrir `phase="migrating"` mas só testava `pending-manual` e o flock (bug
real, não só de documentação — `grep` confirmou zero leitores de
`"migrating"`), e a janela de tempo do `round_in_flight` media a idade do
**disparo** da rodada (mtime do `request.md`), não a inatividade — medido
contra 364 rodadas reais concluídas, 9 (2,5%) já ultrapassavam qualquer
janela fixa razoável (a mais lenta levou 1365s).

A `herdr-8` também respondeu à pergunta sobre a limitação residual descrita
antes desta versão (dispatchers não seguram lock durante toda a própria
operação): os dois revisores concordaram em **não** construir um lock
leitor/escritor. O argumento decisivo (herdr-rev-2): o cenário perigoso é
justamente quando o dispatcher **não está mais vivo** (interrompido, morto,
ou já retornou com os revisores ainda trabalhando) — um lock preso ao
processo do dispatcher não cobre esse caso de jeito nenhum. O que cobre a
duração inteira de uma rodada, independente de quem está vivo, é o
marcador em disco (`find_in_flight_round`, agora baseado no `agent_status`
ao vivo do revisor, não em mtime) — por isso ele é a peça que teve que ser
corrigida, não um lock novo.
"""
import fcntl
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import _herdr_dispatch as core

HERDR = core.HERDR
CLI_TIMEOUT_S = core.CLI_TIMEOUT_S

# rev2_kind default: "claude" (Opus) é a política padrão; Grok foi usado como
# fallback temporário enquanto a cota semanal do Opus estava estourada
# (até 2026-08-30, ver docs/herdr-review-orchestration.config.json no
# claude-bridge) — normalizou, voltou pra Claude. Um space específico pode
# ter rev2_kind diferente gravado no seu próprio migration-state.json a
# qualquer momento; isto é só o default pra quando o arquivo não existe.
_DEFAULT_STATE = {
    "phase": "legacy",
    "rev2_kind": "claude",
    "updated_at": None,
    "note": None,
    # Marcador de proveniência do rename (achado P1-2 herdr-15, herdr-rev):
    # o `agent_session` de `<slug>-rev` capturado por `herdr-migrate-rev`
    # ANTES de qualquer tentativa de rename, pra self-heal exigir prova de
    # que uma migração REAL começou por este mecanismo (não uma coincidência
    # entre duas leituras feitas depois do fato). None = nenhuma migração em
    # andamento por este mecanismo agora (ou já concluída/desistida).
    "pending_rename_session": None,
}


def migration_state_path(cwd):
    return os.path.join(cwd, ".herdr", "migration-state.json")


def migration_lock_path(cwd):
    return os.path.join(cwd, ".herdr", "migration.lock")


def read_migration_state(cwd):
    """Ausência do arquivo == phase 'legacy' (compatível com todo space não
    tocado por esta migração ainda)."""
    path = migration_state_path(cwd)
    if not os.path.isfile(path):
        return dict(_DEFAULT_STATE)
    with open(path) as f:
        state = json.load(f)
    merged = dict(_DEFAULT_STATE)
    merged.update(state)
    return merged


def write_migration_state_atomic(cwd, patch):
    """Escreve em arquivo temporário no mesmo diretório e usa os.replace() —
    nunca deixa migration-state.json parcialmente escrito, mesmo se o
    processo morrer no meio (os.replace é atômico dentro do mesmo
    filesystem).

    `patch` é aplicado por CIMA do estado atual em disco (lido de novo aqui,
    não do default) — achado P2-1 herdr-7: um `patch` parcial (ex: só
    {"phase": "legacy"} na reversão) não pode apagar campos não-default já
    persistidos (ex: rev2_kind="grok" de um space específico) só porque eles
    não foram citados. Passe só os campos que você quer de fato mudar."""
    path = migration_state_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    merged = read_migration_state(cwd)
    merged.update(patch)
    merged["updated_at"] = core.now_iso()
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)
    return merged


def acquire_migration_lock(cwd, owner=None):
    """Exclusão mútua real via `flock(LOCK_EX|LOCK_NB)` — preso ao file
    descriptor do processo que a detém, não à existência do arquivo em
    disco. Isso elimina de raiz a categoria "lock stale" e a corrida de
    quebrá-lo manualmente (achado P1 herdr-7: ler+remover+recriar em
    operações separadas permitia dois processos acreditarem que detêm o
    lock ao mesmo tempo): se o processo morrer (crash, SIGKILL, exit), o
    kernel libera o flock sozinho, sem heurística de idade/pid.

    O arquivo em si guarda só metadata legível (pid/hostname/ts) pra
    diagnóstico — não é a fonte de exclusão mútua.

    Retorna um handle opaco (o fd aberto) se adquiriu — precisa ser passado
    pra `release_migration_lock` (fechar o fd é o que libera o flock).
    Retorna None se outro processo vivo já detém o lock agora."""
    lock_path = migration_lock_path(cwd)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    payload = dict(owner) if owner else {"pid": os.getpid(), "hostname": os.uname().nodename}
    payload["ts"] = time.time()
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(payload).encode())
    os.fsync(fd)
    return fd


def release_migration_lock(handle):
    """Libera o flock fechando `handle` (o fd devolvido por
    `acquire_migration_lock`) — fechar o fd é o que libera o flock.

    Não existe (nem deveria existir) uma variante "sem handle" que libere o
    lock de outro processo: `flock` é preso à *open file description* que o
    adquiriu, não ao path nem ao processo — um fd novo, mesmo pro mesmo
    arquivo e no mesmo processo, é uma open file description DIFERENTE, e
    `flock(LOCK_UN)` nele não afeta o lock adquirido por outro fd. Uma
    versão anterior desta função tentava isso e não fazia nada de verdade
    (bug encontrado durante a correção da herdr-7 — nunca testado
    isoladamente porque o teste também não capturava o handle certo)."""
    try:
        os.close(handle)
    except OSError:
        pass


def is_locked(cwd):
    """Consulta sem criar nada em disco (achado P2-1 herdr-8: a versão
    anterior fazia `os.makedirs`/`os.open(O_CREAT, ...)` mesmo só pra
    perguntar, materializando `.herdr/migration.lock` fantasma em cwds que
    nem existem — real pro `MFC` quando `HERDR_MFC_CWD` não está setada, e
    pro `claude-bridge`, cujo `.herdr/` é versionado e não está no
    `.gitignore`). Arquivo ausente = não travado, sem abrir nada.

    Quando o arquivo existe, tenta adquirir sem bloquear; se conseguir, não
    estava travado (libera de novo antes de retornar). Mesma primitiva de
    exclusão mútua do `acquire_migration_lock` (achado P2-5 herdr-7: antes
    havia duas noções divergentes do que contava como "travado").

    Falha fechado (retorna True) se a checagem em si não puder ser feita
    (`OSError`/`PermissionError` — ex: filesystem somente leitura, ou sem
    suporte a `flock`) — achado P3-1 herdr-8: um gate de segurança que
    derruba o chamador com stacktrace é pior que um que responde
    "bloqueado"."""
    lock_path = migration_lock_path(cwd)
    if not os.path.isfile(lock_path):
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def space_gate_reason(cwd):
    """Motivo, se houver, pelo qual o space não deve receber
    dispatch/bootstrap/swap agora — ou None se estiver livre:

    - "pending-manual": estado terminal, nunca expira sozinho, só sai por
      ação humana explícita (a `note` gravada por `herdr-migrate-rev`
      explica o motivo).
    - "migrating": uma migração está em andamento (marca durável, sobrevive
      mesmo que o flock se perca — ex: o processo que a escreveu morreu
      antes de gravar o estado final). Achado P1-1 herdr-8: uma versão
      anterior desta checagem só olhava `pending-manual` e o flock, nunca
      `phase == "migrating"` — apesar do próprio `herdr-migrate-rev` gravar
      essa fase precisamente pra ser lida aqui. Sem isso, um crash entre
      gravar "migrating" e gravar o estado final deixava o space
      desprotegido assim que o flock morria com o processo.
    - "unknown-phase": o `migration-state.json` tem uma `phase` que não é
      nenhuma das quatro reconhecidas (`legacy`, `migrating`,
      `pending-manual`, `migrated`) — JSON corrompido/editado à mão/typo.
      Achado P4 herdr-10 (herdr-rev): a versão anterior só reconhecia
      `migrating`/`pending-manual` como bloqueio e tratava QUALQUER outra
      coisa como livre, inclusive uma phase inválida — um marcador de
      segurança corrompido liberava o dispatch em vez de bloquear.

    Achado P2-1 herdr-15 (herdr-rev-2): esta função tratava o flock
    ISOLADO (sem `phase == "migrating"`) como bloqueio ("locked"). Isso
    fazia sentido enquanto o único detentor do lock era o próprio
    `herdr-migrate-rev`, cuja janela entre `acquire_migration_lock` e
    gravar `phase="migrating"` é de dois statements, tipicamente
    microssegundos. Mas a partir da herdr-15, `herdr-worker.ts`
    (claude-bridge) também segura o MESMO lock — pelo `agent.prompt`
    inteiro, até `DEFAULT_TIMEOUT_MS` (2 minutos por padrão) — pra ter
    exclusão mútua real com este migrador. Sob a semântica antiga, isso
    fazia os QUATRO leitores deste módulo (herdr-review-dispatch,
    herdr-ask, herdr-swap, herdr-bootstrap) abortarem com "migração em
    andamento" sempre que um worker rev-1 estivesse simplesmente em uso —
    sem migração nenhuma acontecendo. O lock agora tem dois papéis: pro
    `herdr-migrate-rev`, "detenho o lock" significa "posso migrar com
    segurança" (ele SEGUE usando `acquire_migration_lock`/`is_locked`
    diretamente pra isso). Pros LEITORES (esta função), o que importa é "há
    uma migração", e isso é exatamente o que `phase` já significa — por
    isso não consulta mais `is_locked` aqui. A janela residual (o lock
    detido por `herdr-migrate-rev` antes de escrever `phase="migrating"`)
    não fica descoberta: o próprio `_run`/`_attempt_self_heal` re-checa
    `agent_status` idle/done e `round_in_flight` DEPOIS de adquirir o lock,
    então um dispatch que escapou por essa janela de microssegundos ainda
    é pego ali, não aqui.

    Duas mensagens diferentes existiam pra três estados de natureza
    diferente (achado P2-2 herdr-8); os chamadores devem usar isto pra dar
    uma mensagem específica, não "lock ativo — tente de novo depois" pra
    tudo (isso é ativamente enganoso em pending-manual, que não é
    transitório)."""
    phase = read_migration_state(cwd).get("phase")
    if phase == "pending-manual":
        return "pending-manual"
    if phase == "migrating":
        return "migrating"
    if phase not in ("legacy", "migrated"):
        return "unknown-phase"
    return None


def space_is_gated(cwd):
    return space_gate_reason(cwd) is not None


def space_gate_message(cwd, slug):
    """Mensagem pronta pra stderr, específica por motivo (achado P2-2
    herdr-8) — ou None se o space não estiver bloqueado. `pending-manual`
    inclui a `note` gravada, já que "tente de novo depois" é enganoso pra
    um estado que só sai por ação humana."""
    reason = space_gate_reason(cwd)
    if reason is None:
        return None
    if reason == "pending-manual":
        note = read_migration_state(cwd).get("note")
        return (
            f"space '{slug}' está em phase=pending-manual — precisa de "
            f"intervenção manual, não vai se resolver sozinho tentando de "
            f"novo (nota: {note!r})"
        )
    if reason == "migrating":
        return (
            f"space '{slug}' tem uma migração de nome em andamento (phase=migrating) — "
            f"tente de novo depois; se persistir e você suspeitar de um processo morto "
            f"no meio, rode `herdr-migrate-rev {slug}` de novo (ele completa o registro "
            f"sozinho se o rename já tiver aplicado, achado herdr-9)"
        )
    # "unknown-phase" é o único motivo restante (achado P2-1 herdr-15:
    # "locked" deixou de existir como motivo de gate — ver space_gate_reason).
    phase = read_migration_state(cwd).get("phase")
    return (
        f"space '{slug}' tem migration-state.json com phase={phase!r}, que não é "
        f"reconhecida (esperado legacy/migrating/pending-manual/migrated) — "
        f"falhando fechado; verifique o arquivo manualmente"
    )


def agent_truly_absent(exc):
    """True só se `exc` for o erro estruturado `agent_not_found` da API do
    Herdr — qualquer outro texto (timeout, servidor travado, JSON inválido)
    é falha de infraestrutura, não confirmação de ausência (achado P2-1
    herdr-9/herdr-11: `core.agent_status_safe` e um `except RuntimeError`
    genérico apagam essa distinção por construção, em pontos que decidem
    se uma migração é reconciliada ou uma rodada é liberada)."""
    return "agent_not_found" in str(exc)


def agent_status_or_raise(name):
    """Como `core.agent_status_safe`, mas só devolve `None` quando o erro é
    `agent_not_found` — qualquer outro erro é PROPAGADO em vez de virar
    `False` silenciosamente. Use isto (não `agent_status_safe`) em qualquer
    ponto onde a resposta decide se uma migração reconcilia, uma rodada é
    liberada, ou um estado ambíguo é declarado — os lugares onde a
    diferença entre "não existe" e "não consegui perguntar" importa de
    verdade."""
    try:
        return core.agent_status(name)
    except RuntimeError as exc:
        if agent_truly_absent(exc):
            return None
        raise
    except OSError as exc:
        # Achado P3-1 herdr-12: core.api só converte subprocess.TimeoutExpired
        # em RuntimeError — um binário ausente ou sem permissão de execução
        # levanta FileNotFoundError/PermissionError (subclasses de OSError),
        # que escapava sem tratamento aqui e virava stacktraceback bruto nos
        # chamadores (todos esperam RuntimeError). Reembala como RuntimeError
        # pra entrar no mesmo tratamento fail-closed dos outros erros de
        # infra, em vez de atravessar tudo cru.
        raise RuntimeError(f"falha executando o CLI do Herdr pra '{name}': {exc}") from exc


def resolve_reviewer_name(cwd, slug):
    """Dual-read do primeiro revisor: <slug>-rev-1 se vivo, senão <slug>-rev.
    Fail-closed (RuntimeError) se os dois estiverem vivos ao mesmo tempo
    (estado ambíguo — nunca escolhe sozinho) ou se nenhum existir.

    Achado P1 herdr-13 (herdr-rev): usava `core.agent_status_safe`, que
    converte QUALQUER RuntimeError (inclusive timeout/infra) em `False` —
    isto é o último ponto decisório do mecanismo que ainda fazia isso. Se a
    consulta de um dos dois nomes sofrer timeout enquanto o outro responde,
    o timeout virava silenciosamente "não existe", e a função escolhia o
    nome que respondeu como se o estado não fosse ambíguo — exatamente a
    garantia que este docstring promete e que `agent_status_or_raise`
    existe para preservar. Usa `agent_status_or_raise` agora: uma falha de
    infra em qualquer uma das duas consultas propaga (fail-closed), em vez
    de ser tratada como ausência confirmada."""
    rev1_name = f"{slug}-rev-1"
    rev_name = f"{slug}-rev"
    rev1_alive = agent_status_or_raise(rev1_name) is not None
    rev_alive = agent_status_or_raise(rev_name) is not None
    if rev1_alive and rev_alive:
        raise RuntimeError(
            f"estado ambíguo: '{rev1_name}' e '{rev_name}' estão vivos ao mesmo "
            "tempo — recusando resolver sozinho, corrija manualmente antes de "
            "despachar"
        )
    if rev1_alive:
        return rev1_name
    if rev_alive:
        return rev_name
    raise RuntimeError(
        f"nem '{rev1_name}' nem '{rev_name}' existem — space sem primeiro "
        "revisor configurado"
    )


def _artifact_dirs(space_root, namespace):
    base = os.path.join(space_root, ".herdr", namespace)
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, d) for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d))]


_TERMINAL_ARTIFACT = {"review": "verdict.md", "ask": "answer.md"}

# Janela curta só pra cobrir a largada: entre o dispatcher escrever
# request.md e o agent efetivamente entrar em "working" pelo agent.prompt,
# o agent ainda está idle/done legitimamente (achado P1 herdr-9, confirmado
# no próprio docstring de dispatch_and_wait_all: uma leitura pontual de
# status não distingue "idle porque nada foi disparado" de "idle porque
# ainda não processou o que acabou de receber" — é por isso que o
# dispatcher usa `agent prompt --wait` em vez de `agent wait` sozinho).
# Não precisa ser grande: só cobre o tempo entre escrever o request.md e o
# primeiro agent.prompt, não a rodada inteira (isso é o agent_status).
_DISPATCH_STARTUP_GRACE_S = 60


def find_in_flight_round(space_root):
    """Uma rodada conta como 'em voo' se existir <round>/<name>/request.md
    sem o artefato terminal irmão (verdict.md p/ review, answer.md p/ ask)
    E (o `agent_status` AO VIVO daquele revisor não for `idle`/`done` OU o
    `request.md` foi escrito há menos de `_DISPATCH_STARTUP_GRACE_S`).

    Histórico dos sinais tentados (achado de fecho, herdr-9): cada versão
    trocou um falso por outro, nunca somou os dois. `metrics.json` (herdr-6)
    → falso-negativo. Sem janela (herdr-7) → falso-positivo permanente.
    Janela fixa de 1200s medindo o DISPARO (herdr-8) → falso-negativo na
    cauda (rodadas lentas; medido: 9 de 364 rodadas reais ultrapassavam
    qualquer janela fixa razoável). `agent_status` sozinho (herdr-9, v1)
    → falso-negativo na largada (o instante entre escrever request.md e o
    agent entrar em working). A correção final é a DISJUNÇÃO: `agent_status`
    cobre a cauda (não importa há quanto tempo começou), o grace period
    curto cobre a largada (só o intervalo de dispatch, não a rodada
    inteira) — cada um falha exatamente onde o outro funciona.

    `metrics.json` NUNCA entra nessa checagem — sua ausência não significa
    rodada em voo (achado herdr-6). O nome do diretório É o nome do agent (o
    dispatcher grava `verdict_dirs[name] = round_dir/name`), então não
    precisa de mapeamento extra pra descobrir quem perguntar.

    Falha de infraestrutura ao consultar o agent (timeout, servidor
    travado, JSON inválido) NÃO é tratada como "agent não existe" (achado
    P2-1 herdr-9: um `except RuntimeError` genérico igualava as duas coisas,
    fail-open justo quando a consulta em si já não é confiável). Só um erro
    estruturado `agent_not_found` da API do Herdr conta como "nada a esperar
    por ele"; qualquer outro erro falha fechado (bloqueia).

    Retorna o path do `reviewer_dir` bloqueador (diagnóstico — achado P1-1
    herdr-7: "qual diretório" faltava na versão original), ou None se nada
    estiver em voo."""
    now = time.time()
    for namespace, terminal_name in _TERMINAL_ARTIFACT.items():
        for round_dir in _artifact_dirs(space_root, namespace):
            for entry in os.listdir(round_dir):
                reviewer_dir = os.path.join(round_dir, entry)
                if not os.path.isdir(reviewer_dir):
                    continue
                request_path = os.path.join(reviewer_dir, "request.md")
                terminal_path = os.path.join(reviewer_dir, terminal_name)
                if not os.path.isfile(request_path) or os.path.isfile(terminal_path):
                    continue
                if now - os.path.getmtime(request_path) < _DISPATCH_STARTUP_GRACE_S:
                    return reviewer_dir
                try:
                    status = agent_status_or_raise(entry)
                except RuntimeError:
                    return reviewer_dir  # falha de infra: fail-closed, não assume liberado
                if status is None:
                    continue  # agent_not_found confirmado - nada a esperar
                if status not in ("idle", "done"):
                    return reviewer_dir
    return None


def round_in_flight(space_root):
    return find_in_flight_round(space_root) is not None


def resolve_historical_artifact(round_dir, name, filename):
    """Contrato de leitura histórica: tenta round_dir/name/filename; se o
    nome for do papel 'rev' (-rev ou -rev-1), cai pro nome irmão histórico
    (-rev-1 <-> -rev) quando o primeiro não existir. Fail-closed se os DOIS
    existirem (estado ambíguo — nunca escolhe sozinho). `-rev-2` nunca muda
    de nome, então não tem par alternativo."""
    primary = os.path.join(round_dir, name, filename)
    alt_name = None
    if name.endswith("-rev-1"):
        alt_name = name[: -len("-1")]
    elif name.endswith("-rev"):
        alt_name = f"{name}-1"
    alt = os.path.join(round_dir, alt_name, filename) if alt_name else None

    primary_exists = os.path.isfile(primary)
    alt_exists = alt is not None and os.path.isfile(alt)

    if primary_exists and alt_exists:
        raise RuntimeError(
            f"estado ambíguo: existe {filename} tanto em '{name}' quanto em "
            f"'{alt_name}' dentro de {round_dir} — recusando escolher sozinho"
        )
    if primary_exists:
        return primary
    if alt_exists:
        return alt
    raise RuntimeError(
        f"{filename} não encontrado em '{name}'"
        + (f" nem em '{alt_name}'" if alt_name else "")
        + f" dentro de {round_dir}"
    )


def extra_args_for_rev2(kind):
    """Acopla kind -> flags específicas, pra herdr-bootstrap nunca passar um
    flag de um kind pro `agent start` de outro (achado herdr-6: trocar só o
    valor de `kind` por `rev2_kind` sem isto produz `--kind grok -- --model
    opus`, um flag de Claude num agent Grok)."""
    if kind == "claude":
        return ["--model", "opus"]
    return []
