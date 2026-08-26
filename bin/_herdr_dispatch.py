"""Mecânica compartilhada de despacho cego/paralelo/isolado entre
herdr-review-dispatch (revisão de código) e herdr-ask (consulta de design
aberta). Não é executável — é importado pelos dois via sys.path, já que
vivem no mesmo diretório `bin/` sem extensão `.py`.

O que é compartilhado aqui é a MECÂNICA (chamar o `herdr`, congelar contexto,
numerar rodada, despachar e esperar assentar) — o formato do protocolo/prompt
e a semântica de resposta continuam em cada entrypoint, porque revisão de
código e consulta de design produzem respostas estruturalmente diferentes
(lista de achados atômicos vs. posição com premissas) e forçar as duas no
mesmo texto pesa mais do que ajuda.
"""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

HERDR = os.path.expanduser("~/.local/bin/herdr")
CLI_TIMEOUT_S = 30  # teto por chamada individual ao binario herdr, nao pelo ciclo inteiro
BLOCKED_GRACE_S = 15  # quanto tempo em blocked sustentado ate reportar sem esperar o --timeout inteiro


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def api(*args):
    try:
        out = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=CLI_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{' '.join(args)}: sem resposta em {CLI_TIMEOUT_S}s (server do Herdr travado?)")
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {out.stderr.strip() or out.stdout.strip()}")
    return json.loads(out.stdout)["result"]


def get_agent_info(name):
    return api("agent", "get", name)["agent"]


def agent_status(name):
    return get_agent_info(name)["agent_status"]


def agent_status_safe(name):
    try:
        agent_status(name)
        return True
    except RuntimeError:
        return False


def agent_cwd(name):
    return get_agent_info(name)["cwd"]


def next_round_dir(cwd, slug, namespace="review"):
    """Numera e cria o diretório da próxima rodada em `.herdr/<namespace>/`.

    namespace separa o espaço de numeração entre modos (ex: "review" vs.
    "ask") — sem isso, uma consulta de design intercalada com rodadas de
    revisão bagunçaria o encadeamento de `--verify`, que lê head_sha da
    rodada anterior pelo número.
    """
    base = os.path.join(cwd, ".herdr", namespace)
    os.makedirs(base, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(slug)}-(\d+)$")
    nums = [int(m.group(1)) for d in os.listdir(base) if (m := pattern.match(d))]
    n = max(nums, default=0) + 1
    round_dir = os.path.join(base, f"{slug}-{n}")
    os.makedirs(round_dir, exist_ok=False)
    return round_dir, n


def freeze_files(files, round_dir):
    """Copia cada arquivo pra round_dir/_snapshot/, preservando o path absoluto
    como subdiretório (sem a '/' inicial). Quem lê deve usar a cópia
    congelada, não o path original no disco — sem isso, o arquivo pode mudar
    no meio da rodada e a resposta sai sobre material que já não existe mais
    (já aconteceu na prática, rodada inteira perdida)."""
    snapshot_root = os.path.join(round_dir, "_snapshot")
    mapping = {}
    for f in files:
        dest = os.path.join(snapshot_root, f.lstrip(os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(f, dest)
        mapping[f] = dest
    return mapping


def dispatch_and_wait_all(prompts, timeout_s):
    """Manda o prompt e espera cada agent assentar (idle/done) via
    `agent prompt --wait`, um subprocesso concorrente por nome — um agent
    lento ou travado não atrasa a detecção dos outros. Ancorado na submissão,
    ao contrário de `agent wait` sozinho, que casa o estado JÁ atual na hora
    da chamada, não uma transição depois do prompt (confirmado ao vivo:
    retornou em ~5ms num agent que só estava idle de antes).

    `blocked` fica de propósito fora do --until: um blip transitório não bate
    o alvo e a espera nativa continua através dele, sem grace period manual.
    Um watchdog em paralelo, por agent, cobre o caso sustentado
    (BLOCKED_GRACE_S) e mata o processo correspondente se achar blocked
    contínuo, sem esperar o --timeout inteiro.

    prompts: dict nome -> texto do prompt.
    Retorna (result, info, settle_ts): dicts nome -> status ("idle"/"done"/
    "blocked"/"timeout"/"stalled"/"error"); nome -> dict do agent quando
    disponível, senão uma mensagem (str) ou None; nome -> timestamp ISO de
    quando resolveu.
    """
    deadline = time.time() + timeout_s
    procs = {
        name: subprocess.Popen(
            [HERDR, "agent", "prompt", name, text,
             "--wait", "--until", "idle", "--until", "done",
             "--timeout", str(timeout_s * 1000)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for name, text in prompts.items()
    }
    result = {name: None for name in procs}
    info = {}
    settle_ts = {}
    blocked_since = dict.fromkeys(procs)

    def finish(name, status, detail):
        result[name] = status
        info[name] = detail
        settle_ts[name] = now_iso()

    while any(v is None for v in result.values()):
        for name, proc in procs.items():
            if result[name] is not None:
                continue
            ret = proc.poll()
            if ret is not None:
                out, err = proc.communicate()
                if ret == 0:
                    try:
                        agent_info = json.loads(out)["result"]["agent"]
                        finish(name, agent_info["agent_status"], agent_info)
                    except (json.JSONDecodeError, KeyError):
                        finish(name, "error", (out or err).strip())
                    continue
                try:
                    payload = json.loads(out or err)
                    code = payload.get("error", {}).get("code")
                    message = payload.get("error", {}).get("message", "")
                except json.JSONDecodeError:
                    code, message = None, (out or err).strip()
                if code == "agent_blocked":
                    finish(name, "blocked", message)
                elif code == "timeout":
                    finish(name, "timeout", message)
                elif code == "agent_prompt_stalled":
                    finish(name, "stalled", message)
                else:
                    finish(name, "error", message or f"exit {ret}")
                continue
            if time.time() >= deadline:
                proc.kill()
                proc.wait()
                finish(name, "timeout", None)
                continue
            try:
                agent_info = get_agent_info(name)
            except RuntimeError:
                agent_info = None
            if agent_info and agent_info["agent_status"] == "blocked":
                if blocked_since[name] is None:
                    blocked_since[name] = time.time()
                elif time.time() - blocked_since[name] >= BLOCKED_GRACE_S:
                    proc.kill()
                    proc.wait()
                    finish(name, "blocked", agent_info)
            else:
                blocked_since[name] = None
        if any(v is None for v in result.values()):
            time.sleep(2)
    return result, info, settle_ts
