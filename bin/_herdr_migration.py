"""Mecânica de migração de nome do primeiro revisor de cada space, de
`<slug>-rev` para `<slug>-rev-1` (`<slug>-rev-2` nunca muda de nome).

Isto é o mecanismo, não a decisão de quando migrar: `herdr-migrate-rev`
(script separado) é o driver que usa estas funções para migrar um space por
vez. `herdr-review-dispatch`, `herdr-ask` e `herdr-bootstrap` só *leem* o
estado escrito aqui — nenhum dispatcher escreve em `migration-state.json`.

Histórico: quatro rodadas de revisão (`herdr-4` a `herdr-7` em
`.herdr/review/`) reprovaram versões anteriores deste mecanismo — as três
primeiras por falta de lock atômico de verdade, identidade de escrita
divergindo da attestation, e `_infer_role` sem `-rev-1`; a quarta (`herdr-7`,
primeira revisão de código real) por: lock baseado em existência de arquivo
com corrida real entre detectar-stale e recriar, `phase=pending-manual` não
respeitado em lugar nenhum, `round_in_flight` sem janela de tempo (trocou um
falso-negativo por um falso-positivo PERMANENTE), e resultado de `agent
rename` tratado como certo sem consultar o que aconteceu de fato.

Limitação residual conhecida (achado herdr-7, não fechada por completo):
`herdr-review-dispatch`/`herdr-ask`/`herdr-swap` checam `space_is_gated()`
uma vez, no início, mas não seguram um lock durante toda a própria operação
(que pode levar até `--timeout`, tipicamente 1200s) — uma migração pode
começar depois dessa checagem e antes do prompt de fato ser enviado. Fechar
isso de vez exigiria um lock leitor/escritor compartilhado entre despacho e
migração, não implementado aqui por escopo; `herdr-migrate-rev` reduz o
dano prático ao re-checar idle/round-em-voo DEPOIS de adquirir o próprio
lock (não antes), e ao nunca assumir sucesso do rename sem consultar os
dois nomes de verdade — mas a janela entre "dispatcher decidiu prosseguir"
e "migração começa" continua existindo.
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

# Janela padrão pra round_in_flight() considerar um request.md órfão como
# "ainda em voo" (achado P1 herdr-7: sem recorte de tempo, uma rodada
# abandonada em qualquer momento do passado trava a migração PRA SEMPRE,
# não só temporariamente — bug pior que o falso-negativo do metrics.json que
# esta checagem substituiu na herdr-6). Bate com o --timeout default dos
# dispatchers.
DEFAULT_IN_FLIGHT_WINDOW_S = 1200

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
    """Tenta adquirir sem bloquear; se conseguir, não estava travado (libera
    de novo antes de retornar True/False). Ao contrário da versão anterior
    (baseada em existência de arquivo + heurística de idade), isto usa a
    MESMA primitiva de exclusão mútua do `acquire_migration_lock` — não há
    mais duas noções divergentes do que conta como "travado" (achado P2-5
    herdr-7)."""
    lock_path = migration_lock_path(cwd)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return False


def space_is_gated(cwd):
    """True se o space não deve receber dispatch/bootstrap/swap agora: uma
    migração está ativamente rodando (flock preso) OU o estado persistido
    exige intervenção manual (`phase == "pending-manual"`, que — ao
    contrário do lock — nunca expira sozinho; só uma ação humana explícita
    muda essa fase). Achado P1-2 herdr-7: nenhum consumidor checava
    `pending-manual`, e o lock antigo baseado em arquivo expirava sozinho em
    LOCK_STALE_S, dissolvendo a própria proteção que o estado deveria
    garantir."""
    if read_migration_state(cwd).get("phase") == "pending-manual":
        return True
    return is_locked(cwd)


def resolve_reviewer_name(cwd, slug):
    """Dual-read do primeiro revisor: <slug>-rev-1 se vivo, senão <slug>-rev.
    Fail-closed (RuntimeError) se os dois estiverem vivos ao mesmo tempo
    (estado ambíguo — nunca escolhe sozinho) ou se nenhum existir."""
    rev1_name = f"{slug}-rev-1"
    rev_name = f"{slug}-rev"
    rev1_alive = core.agent_status_safe(rev1_name)
    rev_alive = core.agent_status_safe(rev_name)
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


def find_in_flight_round(space_root, window_s=DEFAULT_IN_FLIGHT_WINDOW_S):
    """Uma rodada conta como 'em voo' se existir <round>/<name>/request.md
    sem o artefato terminal irmão (verdict.md p/ review, answer.md p/ ask)
    E o `request.md` tiver sido escrito há no máximo `window_s` segundos.

    metrics.json NUNCA entra nessa checagem — sua ausência não significa
    rodada em voo (achado herdr-6: várias rodadas históricas legítimas nunca
    tiveram metrics.json). A janela de tempo existe porque a versão anterior
    desta função (sem janela) trocou aquele falso-negativo por um
    falso-positivo PERMANENTE: uma rodada abandonada em qualquer momento do
    passado (revisor travado, timeout, cancelamento manual) deixa
    `request.md` órfão pra sempre, e sem janela isso bloqueia a migração
    daquele space para sempre também (achado P1-1 herdr-7 — medido: 5 dos 6
    spaces tinham pelo menos uma rodada órfã histórica).

    Retorna o path do `reviewer_dir` bloqueador (pra diagnóstico — "qual
    diretório" é exatamente o que faltava na versão anterior), ou None se
    nada estiver em voo."""
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
                age = now - os.path.getmtime(request_path)
                if age <= window_s:
                    return reviewer_dir
    return None


def round_in_flight(space_root, window_s=DEFAULT_IN_FLIGHT_WINDOW_S):
    return find_in_flight_round(space_root, window_s) is not None


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
