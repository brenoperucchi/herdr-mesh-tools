"""Acorda o exec dono da rodada quando o ultimo parecer chega (pedido do coordenador, 11:50).

Hoje o exec ficava parado ate 5 min esperando o ciclo do vigia (8 casos em 18/11). O dispatcher,
que ja roda como filho do exec, avisa no fim da rodada.

Por que `agent prompt` e nao `herdr-message send --notice` (a preferencia do pedido): o dispatcher roda
no pane do PROPRIO exec, entao o remetente provado seria o exec e o destino tambem; o ledger recusa
(`self_send_refused`, _herdr_message_ledger.py). Um "nome proprio do dispatcher" nao existe no registro
do Herdr (a prova e' Runtime.caller() pela ancestria de processo, que devolve o pane do exec), entao
nao ha remetente distinto sem afrouxar a prova de identidade do mesh.
A entrega usa o mesmo `core.api` do resto do dispatcher: herdr-can-send=0 imediatamente antes (e a
validacao de grupo), sem --allow-working, sem tocar rascunho humano. Falha nunca derruba a rodada:
vira `wake` nas metrics, e o vigia segue como contingencia (avisa em 10 min).
"""
import contextlib
import json
import os
import re

_VERDICT = re.compile(r"\b(APPROVE|REQUEST_CHANGES|BLOCKED|COMMENT)\b")


def verdict_head(path):
    """Primeira palavra de veredito nas 15 primeiras linhas do verdict.md; senao um marcador."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(15), f):
                m = _VERDICT.search(line)
                if m:
                    return m.group(1)
    except OSError:
        return "sem parecer"
    return "veredito ilegível"


def build_message(round_dir, review_status, target_names, successful, slug):
    parts = []
    for name in target_names:
        short = name[len(slug) + 1:] if name.startswith(slug + "-") else name
        if name in successful:
            parts.append(f"{short}={verdict_head(os.path.join(round_dir, name, 'verdict.md'))}")
        else:
            parts.append(f"{short}=sem parecer utilizável")
    return (f"[herdr-review] rodada {os.path.basename(round_dir)} {review_status}: "
            f"{', '.join(parts)}, pareceres em {round_dir}")


WAKE_WAIT_S = 300        # teto da espera pelo exec terminar o proprio turno (herdr-can-send = 3)
WAKE_QUIET_S = 2.0


def wake_exec(exec_name, text, core, *, own_pane, wait_s=WAKE_WAIT_S):
    """Aviso com uma nova tentativa quando o exec ainda esta no proprio turno.

    herdr-can-send 3 (working): nao se escreve por cima de um turno, entao espera o exec ficar idle/done
    (core.wait_quiescent, teto `wait_s`) e envia UMA vez (cerebro-210/212: o exec parava 15 a 60 s depois e
    ficava parado ate o vigia lembrar). Codigos 1/2 (rascunho humano, dialogo) e qualquer outra recusa:
    nada enviado, sem espera, registrado em `can_send_code`. Nunca levanta."""
    trace = {}
    result = _wake_once(exec_name, text, core, own_pane=own_pane, trace=trace)
    if result.get("status") == "refused" and result.get("can_send_code") == 3:
        try:
            core.wait_quiescent(exec_name, quiet_s=WAKE_QUIET_S, timeout_s=wait_s)
        except Exception as exc:  # noqa: BLE001 - timeout, blocked ou agent sumiu: nada enviado
            return {"status": "refused", "can_send_code": 3, "waited": True,
                    "reason": f"exec não ficou idle em {wait_s}s ({str(exc)[:160]}); nada enviado"}
        # a referencia de identidade e' a do dono observado ANTES da espera (r204/rev-1): outro ocupante do
        # mesmo pane/sessao/terminal que apareceu durante a espera nunca vira o dono autorizado
        result = _wake_once(exec_name, text, core, own_pane=own_pane, expected=trace.get("info"))
        result["retried_after_working"] = True
    return result


def _wake_once(exec_name, text, core, *, own_pane, expected=None, trace=None):
    """Uma tentativa. Devolve dict(status, ...). Nunca levanta (qualquer falha vira resultado estruturado).

    Mantem a identidade do dono observada ate a submissao: reserva de submissao do core,
    reobservacao e comparacao nome/pane/terminal/sessao; outro ocupante do pane recusa."""
    try:
        info = core.get_agent_info(exec_name)
        if trace is not None:
            trace["info"] = info
        pane = info.get("pane_id")
        if not own_pane or pane != own_pane:
            return {"status": "skipped", "reason": "dispatcher não roda no pane do exec dono da rodada"}
        if expected is not None and (info.get("name") != expected.get("name")
                                     or info.get("terminal_id") != expected.get("terminal_id")
                                     or core._dispatch_differences(expected, info, include_lifecycle=False)):
            return {"status": "refused", "reason": "identidade do exec mudou durante a espera; nada enviado"}
        with core._dispatch_submission_locks({exec_name: info}):
            now = core.get_agent_info(exec_name)
            # `_dispatch_differences` do core nao inclui terminal_id: comparado aqui, explicitamente.
            if (now.get("name") != exec_name or now.get("terminal_id") != info.get("terminal_id")
                    or core._dispatch_differences(info, now, include_lifecycle=False)):
                return {"status": "refused", "reason": "identidade do exec mudou antes do envio; nada enviado"}
            try:
                msg = text() if callable(text) else text
            except Exception:  # noqa: BLE001 - nada foi enviado: rotulo proprio, nao "unknown"
                return {"status": "error", "reason": "métricas ilegíveis; nada enviado"}
            try:
                core.api("agent", "prompt", pane, msg)
            except (OSError, ValueError) as exc:
                # O transporte pode ter entregue: resultado desconhecido, sem reenvio automatico.
                return {"status": "unknown", "reason": f"{type(exc).__name__}; sem reenvio"}
        return {"status": "delivered", "pane_id": pane}
    except RuntimeError as exc:  # inclui DeliveryRefusedError/GroupBindingRefusedError
        out = {"status": "refused", "reason": str(exc)[:300]}
        if getattr(exc, "code", None) is not None:
            out["can_send_code"] = exc.code
        return out
    except Exception as exc:  # noqa: BLE001 - opcional: nunca derruba a rodada
        return {"status": "error", "reason": type(exc).__name__}


def _merge_wake(path, wake, core):
    """Relê o metrics.json ATUAL e grava so o campo `wake` (nunca restaura uma copia antiga)."""
    with open(path) as f:
        metrics = json.load(f)
    metrics["wake"] = wake
    core.write_json_atomic(path, metrics)


def wake_round(round_dir, slug, core, coordinator, *, own_pane, publish=None):
    """Aviso a partir do metrics.json ja publicado (rodada normal e --complementar). O texto e' montado de
    novo a cada tentativa a partir das metricas ATUAIS; `publish(wake)` grava o resultado (o dispatcher passa um
    publicador protegido pela reserva do space; por padrao relê e mescla o arquivo). Nunca levanta."""
    try:
        path = os.path.join(round_dir, "metrics.json")

        def text():
            with open(path) as f:
                metrics = json.load(f)
            names = list(metrics.get("reviewers", {}))
            ok = [n for n in names if metrics["reviewers"][n].get("verdict_sha256")]
            return build_message(round_dir, metrics.get("review_status", "?"), names, ok, slug)

        text()  # falha cedo (sem metricas validas) em vez de esperar o exec por nada
        wake = wake_exec(coordinator, text, core, own_pane=own_pane)
        try:
            (publish or (lambda w: _merge_wake(path, w, core)))(wake)
        except Exception:  # noqa: BLE001
            pass
        return wake
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": type(exc).__name__}


@contextlib.contextmanager
def caller_context(environ, runtime_factory=None):
    """Rende o pane do dispatcher E mantem a rota da prova durante o aviso.

    Achado r188/rev-1 P2: Runtime recupera socket/config/sessao dos ancestrais quando o executor
    remove as variaveis, mas o core consulta e envia pelo os.environ. Com duas sessoes Herdr, a prova
    seria da sessao A e a entrega na B. Aqui as dicas de rota recuperadas pelo Runtime que faltam em
    `environ` ficam em os.environ so durante o aviso e sao restauradas ao sair, de modo que prova,
    consulta do dono, guarda e submissao usam o MESMO servidor. Sem prova: pane None (skipped).
    HERDR_PANE_ID presente dispensa o Runtime (rota ja e' a do proprio ambiente)."""
    pane = environ.get("HERDR_PANE_ID")
    if pane:
        yield pane
        return
    applied = {}
    try:
        if runtime_factory is None:
            from _herdr_message_runtime import Runtime
            runtime_factory = Runtime
        runtime = runtime_factory()
        proved = runtime.caller().get("pane_id")
        recovered = getattr(runtime, "env", {}) or {}
        for key in ("HERDR_SOCKET_PATH", "HERDR_CONFIG_PATH", "HERDR_SESSION"):
            if key in recovered and key not in os.environ:
                applied[key] = recovered[key]
    except Exception:  # noqa: BLE001 - sem prova, sem aviso
        yield None
        return
    for key, value in applied.items():
        os.environ[key] = value
    try:
        yield proved
    finally:
        for key in applied:
            os.environ.pop(key, None)


@contextlib.contextmanager
def proven_caller_context(environ, runtime_factory=None):
    """Como `caller_context`, mas para AUTORIZACAO: so a prova nativa vale (achado r191 rev-1/rev-2).

    HERDR_PANE_ID e' dado controlavel pelo processo; aqui e' so uma pista que precisa COINCIDIR com o
    pane provado por Runtime.caller() (ancestria de processo). Sem prova, ou com divergencia, devolve
    None (= nao comprovado = recusa). A rota recuperada pela prova fica em os.environ ate o fim do
    bloco (o chamador mantem o bloco aberto durante toda a operacao) e e' restaurada ao sair."""
    applied = {}
    try:
        if runtime_factory is None:
            from _herdr_message_runtime import Runtime
            runtime_factory = Runtime
        runtime = runtime_factory()
        proved = runtime.caller().get("pane_id")
        hint = environ.get("HERDR_PANE_ID")
        if not proved or (hint and hint != proved):
            raise RuntimeError("caller nao comprovado ou pista divergente")
        recovered = getattr(runtime, "env", {}) or {}
        for key in ("HERDR_SOCKET_PATH", "HERDR_CONFIG_PATH", "HERDR_SESSION"):
            if key in recovered and key not in os.environ:
                applied[key] = recovered[key]
    except Exception:  # noqa: BLE001
        yield None
        return
    for key, value in applied.items():
        os.environ[key] = value
    try:
        yield proved
    finally:
        for key in applied:
            os.environ.pop(key, None)
