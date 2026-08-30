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


# Desliga o sistema nativo de sub-agentes do Codex (`agents`/`pipeline_*` em
# ~/.codex/config.toml, global à máquina) pra qualquer agent que o Herdr
# gerencia. Achado real (2026-08-29, omabackup-exec pós-swap): um exec Codex
# tentou "uma rodada final independente" via esse mecanismo nativo em vez de
# voltar pro herdr-review — spawnou um processo efêmero, invisível, fora do
# Herdr, sem pane, sem identidade, e falhou porque o `./agents/*.toml` local
# não existe no projeto. Ver AGENTS.md "Reviewer colleagues" / herdr-6.
#
# `max_threads=0` (usado até 2026-08-30) quebrou depois de um update do
# Codex (0.149.1 -> 0.151.0): a chave de validação real é
# `agents.max_concurrent_threads_per_session`, que exige >=1 — `0` vira erro
# fatal de config, o processo nem sobe ("Error loading configuration").
# `max_depth=0` consegue o mesmo efeito (nenhum sub-agente pode ser
# spawnado, profundidade zero) sem bater nesse piso.
CODEX_NO_NATIVE_AGENTS = ["-c", "agents.max_depth=0"]

ROLE_REINFORCEMENT_PROMPT = """Reforço de papel — mandatório a cada início ou troca de agent neste space,
não é um FYI opcional.

Você é `{name}` no space `{slug}` (cwd `{cwd}`), rodando dentro do Herdr
(mesh de panes/tabs/workspaces que conecta múltiplos agents de CLIs
diferentes). Seus colegas neste mesmo space são panes REAIS e vivos no Herdr
agora — não hipotéticos, nada a configurar: {siblings}.

Regra que não muda entre CLI (Claude ou Codex) nem entre troca de kind: TODA
revisão, consulta de design ou "segunda opinião" passa pelas skills
`herdr-review`/`herdr-ask`, que despacham pros panes acima. NUNCA use o
sistema nativo de sub-agentes do seu próprio CLI (ex: `agents`/
`pipeline_reviewer` do Codex, ou o tool `Agent` do Claude) como substituto —
isso spawna processo efêmero e invisível, fora do Herdr, sem pane, sem
identidade persistente; o usuário não consegue ver nem confiar no resultado.
Se der vontade de "fazer mais uma rodada rápida e independente por conta
própria", isso é sinal de voltar pro `herdr-review` de novo, não de usar um
atalho nativo do seu CLI.

Você tem o CLI `herdr` disponível (mesmas ferramentas que eu uso pra
gerenciar você) — não é só pra ser gerenciado, é pra você também usar: se
precisar reorganizar seu próprio layout (ex: mover seu pane pra uma tab
própria, dividir um pane novo), rode `herdr --help` / `herdr <comando>
--help` você mesmo em vez de travar sem saber o comando ou pedir pro usuário
fazer na mão — `herdr pane move/split/close`, `herdr tab create`, `herdr
agent rename` cobrem a maioria dos casos. `herdr --skill` traz o guia
completo se precisar de mais contexto.

Detalhes completos em AGENTS.md/CLAUDE.md (seção "Reviewer colleagues") e em
`.herdr/reviewer.md` deste repo, se existir."""

SCOUT_ROLE_NOTE = """

Nota específica de papel — você é `{name}`, o scout deste space: seu perfil é
READ-ONLY de propósito, sem acesso de escrita ao socket do Herdr (mover pane,
criar tab, etc. vão dar "Operation not permitted" — isso é o sandbox
funcionando certo, não um bug). Se esbarrar numa ação que precisa de escrita,
**não tente contornar o read-only** e não fique só descrevendo o problema —
mande a ação específica direto pro `{exec_name}` (ele tem escrita) pedindo
pra executar por você, e siga em frente com o que puder fazer sem escrita
enquanto isso."""


def _infer_role(name):
    if name.endswith("-rev-2"):
        return "rev-2"
    if name.endswith("-rev"):
        return "rev"
    if name.endswith("-scout"):
        return "scout"
    if name.endswith("-exec"):
        return "exec"
    return None


def role_reinforcement_prompt(name, slug, cwd, siblings):
    """siblings: lista de nomes dos outros agents do mesmo space (`<slug>-exec`,
    `<slug>-rev`, `<slug>-rev-2`), já sem o próprio `name`. Formata como texto
    corrido pro prompt; lista vazia (space sem colegas, ex. "herdr" sem exec)
    vira uma frase dizendo isso explicitamente, não um "{siblings}" vazio."""
    sib_text = ", ".join(f"`{s}`" for s in siblings) if siblings else "nenhum — este space não tem outros agents"
    prompt = ROLE_REINFORCEMENT_PROMPT.format(name=name, slug=slug, cwd=cwd, siblings=sib_text)
    if _infer_role(name) == "scout":
        exec_name = f"{slug}-exec"
        prompt += SCOUT_ROLE_NOTE.format(name=name, exec_name=exec_name)
    return prompt


_COMPOSE_LINE_RE = re.compile(r"^(❯|›)\s+(\S.*)$")
_PENDING_MARKERS = (
    "interrupted", "what should claude do instead", "do you trust",
    "confia", "trust the contents",
)


def pane_looks_busy_with_human_input(pane_id, lines=12):
    """Heurística, não garantia: lê o pane (`--source detection`, o buffer
    que o próprio Herdr usa pra detectar estado de agent, onde a caixa de
    composição vive) e sinaliza suspeita de texto humano não confirmado ou
    diálogo pendente. Existe uma corrida real que isso não fecha: texto pode
    chegar ENTRE essa leitura e a ação seguinte — reduz a janela, não prova
    segurança. Ver discussão em herdr-6 (herdr-ask) sobre os limites disso.

    Retorna (suspeito: bool, motivo: str|None). Em qualquer erro de leitura,
    trata como suspeito (falha segura, não silenciosa)."""
    try:
        out = subprocess.run(
            [HERDR, "pane", "read", pane_id, "--source", "detection", "--lines", str(lines)],
            capture_output=True, text=True, timeout=CLI_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return True, "timeout lendo o pane pra checar composição — tratando como suspeito"
    if out.returncode != 0:
        return True, f"falha lendo pane pra checar composição: {out.stderr.strip()}"
    text = out.stdout
    for line in text.splitlines():
        m = _COMPOSE_LINE_RE.match(line.strip())
        if m:
            return True, f"caixa de composição parece ter texto não enviado: {m.group(2)[:80]!r}"
    lowered = text.lower()
    for marker in _PENDING_MARKERS:
        if marker in lowered:
            return True, f"pane mostra diálogo/pergunta pendente (marcador: {marker!r})"
    return False, None


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
