"""Mecânica de migração de nome do primeiro revisor de cada space, de
`<slug>-rev` para `<slug>-rev-1` (`<slug>-rev-2` nunca muda de nome).

Isto é o mecanismo, não a decisão de quando migrar: `herdr-migrate-rev`
(script separado) é o driver que usa estas funções para migrar um space por
vez. `herdr-review-dispatch`, `herdr-ask` e `herdr-bootstrap` só *leem* o
estado escrito aqui — nenhum dispatcher escreve em `migration-state.json`.

Histórico: três rodadas de revisão em texto (`herdr-4`, `herdr-5`, `herdr-6`
em `.herdr/review/`) reprovaram versões anteriores deste plano por falta de
lock atômico de verdade, identidade de escrita divergindo da identidade de
attestation, e `_infer_role` não reconhecendo `-rev-1`. Este módulo resolve
os dois primeiros; o terceiro é corrigido em `_herdr_dispatch.py`.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import _herdr_dispatch as core

HERDR = core.HERDR
CLI_TIMEOUT_S = core.CLI_TIMEOUT_S

LOCK_STALE_S = 600  # 10 min sem heartbeat + pid morto = lock stale, permite takeover

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


def write_migration_state_atomic(cwd, state):
    """Escreve em arquivo temporário no mesmo diretório e usa os.replace() —
    nunca deixa migration-state.json parcialmente escrito, mesmo se o
    processo morrer no meio (os.replace é atômico dentro do mesmo
    filesystem)."""
    path = migration_state_path(cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    merged = dict(_DEFAULT_STATE)
    merged.update(state)
    merged["updated_at"] = core.now_iso()
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)
    return merged


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # existe, só não é nosso
    return True


def acquire_migration_lock(cwd, owner=None):
    """Exclusão mútua real via O_CREAT|O_EXCL — falha atomicamente se o
    arquivo já existir (ao contrário do campo `phase`, que é só estado
    legível, não uma primitiva de lock). owner default: {pid, hostname}.

    Retorna True se adquiriu: False se já está travado por outro dono ativo.
    Lock stale (mais de LOCK_STALE_S e pid não existe mais) é destravado
    automaticamente antes da tentativa, com aviso em stderr."""
    lock_path = migration_lock_path(cwd)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    _maybe_break_stale_lock(lock_path)
    payload = owner or {"pid": os.getpid(), "hostname": os.uname().nodename}
    payload["ts"] = time.time()
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return True


def _maybe_break_stale_lock(lock_path):
    if not os.path.isfile(lock_path):
        return
    try:
        with open(lock_path) as f:
            payload = json.load(f)
        pid = payload.get("pid")
        ts = payload.get("ts", 0)
    except (json.JSONDecodeError, OSError):
        return
    age = time.time() - ts
    if age < LOCK_STALE_S:
        return
    if pid is not None and _pid_alive(pid):
        return
    import sys
    print(
        f"aviso: lock de migração em {lock_path} está stale "
        f"(pid {pid} morto, {age:.0f}s sem heartbeat) — destravando",
        file=sys.stderr,
    )
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def release_migration_lock(cwd):
    lock_path = migration_lock_path(cwd)
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def is_locked(cwd):
    return os.path.isfile(migration_lock_path(cwd))


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


def round_in_flight(space_root):
    """Uma rodada conta como 'em voo' se existir <round>/<name>/request.md
    sem o artefato terminal irmão (verdict.md p/ review, answer.md p/ ask).
    metrics.json NUNCA entra nessa checagem — sua ausência não significa
    rodada em voo (achado recorrente nas rodadas herdr-4/5/6: várias rodadas
    históricas legítimas nunca tiveram metrics.json)."""
    for namespace, terminal_name in _TERMINAL_ARTIFACT.items():
        for round_dir in _artifact_dirs(space_root, namespace):
            for entry in os.listdir(round_dir):
                reviewer_dir = os.path.join(round_dir, entry)
                if not os.path.isdir(reviewer_dir):
                    continue
                request_path = os.path.join(reviewer_dir, "request.md")
                terminal_path = os.path.join(reviewer_dir, terminal_name)
                if os.path.isfile(request_path) and not os.path.isfile(terminal_path):
                    return True
    return False


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
