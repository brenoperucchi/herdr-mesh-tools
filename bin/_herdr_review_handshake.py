"""Primeiro turno controlado de um revisor Codex reciclado, antes de uma revisão.

Causa (2026-11-18, cerebro-195 e lastro-rust-280): um Codex reciclado (restart/resume,
atualização) volta com `agent_session` ausente no `herdr agent get` e só ganha sessão nativa
depois do primeiro turno. O dispatcher exige sessão (`review_candidate`), então recusava o
revisor com "sessão do revisor não observável" e a rodada saía parcial: request.md gravado,
nenhum prompt entregue, revisor ocioso.

Este módulo faz uma vez, e só uma, o mesmo aperto de mão do bootstrap
(`role_reinforcement_prompt`): valida identidade, passa pelo can-send, entrega, e espera a
sessão nativa assentar. Recibo durável por processo (terminal, pid, starttime, boot_id) impede
reenvio: só um recibo `aborted_before_send` válido libera nova tentativa; qualquer outro estado,
versão ou formato desconhecido bloqueia como ambíguo. Papéis de grupo (`review_groups`) ficam de fora: têm a hidratação inicial própria.
"""
import json
import os
import subprocess
import time
import uuid

TIMEOUT_S = 120
# Estados em que um envio pode ter acontecido: nunca reenviar sem diagnóstico humano.
BLOCKING = ("delivery_attempted", "delivery_unconfirmed", "ambiguous", "complete")
RECEIPT_VERSION = 1
RETRYABLE = ("aborted_before_send",)  # unico estado que PROVA ausencia de envio


def _native_session(info):
    s = info.get("agent_session")
    return bool(isinstance(s, dict) and s.get("value")
                and s.get("source") == "herdr:codex" and s.get("kind") == "id")


def needs_handshake(info):
    """Codex ocioso, pronto e sem NENHUMA sessão registrada."""
    return (isinstance(info, dict) and info.get("agent") == "codex"
            and info.get("agent_session") is None
            and info.get("interactive_ready") is True
            and info.get("agent_status") in ("idle", "done")
            and bool(info.get("terminal_id")))


def process_key(info, core, root):
    """pid/starttime/boot_id do Codex em foreground: a encarnação física, não o terminal.

    Um restart mantém pane e terminal_id, então só o processo distingue "esta tentativa" de
    "o reciclo seguinte". Não valida o argv (um `codex resume <id>` é legítimo aqui)."""
    data = core.api("pane", "process-info", "--pane", info["pane_id"])["process_info"]
    if data.get("pane_id") != info["pane_id"]:
        raise RuntimeError("process-info resolveu outro pane")
    cands = [p for p in data.get("foreground_processes", [])
             if p.get("name") == "codex" or (p.get("argv") and os.path.basename(p["argv"][0]) == "codex")]
    if len(cands) != 1 or not isinstance(cands[0].get("pid"), int) or cands[0]["pid"] <= 0:
        raise RuntimeError("foreground Codex ausente ou ambíguo")
    pid = cands[0]["pid"]
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
        cwd = os.path.realpath(f"/proc/{pid}/cwd")
    except (OSError, IndexError) as exc:
        raise RuntimeError("não consegui ler o processo Codex") from exc
    if fields[0] in ("T", "t", "Z", "X") or core.project_root(cwd) != root:
        raise RuntimeError("processo Codex não coincide com o foreground/raiz")
    return {"pid": pid, "starttime": fields[19], "boot_id": boot}


def receipt_path(root, name, info, proc):
    return os.path.join(root, ".herdr", "review", "_handshake",
                        f"{name}-{info['terminal_id']}-{proc['pid']}-{proc['starttime']}-{proc['boot_id'][:12]}.json")


def intermediate_receipt_path(root, name, info, proc):
    """Formato da r183 (terminal + pid + starttime, sem boot_id). O pid/starttime atuais ja
    identificam a encarnacao; o recibo antigo so e' achado se for a MESMA."""
    return os.path.join(root, ".herdr", "review", "_handshake",
                        f"{name}-{info['terminal_id']}-{proc['pid']}-{proc['starttime']}.json")


def legacy_receipt_path(root, name, info):
    """Formato da r182 (por terminal). Segue consultado: atualizar o codigo nao autoriza reenvio."""
    return os.path.join(root, ".herdr", "review", "_handshake", f"{name}-{info['terminal_id']}.json")


def _prior_state(path):
    """None se nao ha recibo; senao o estado, ou 'ambiguous' se ilegivel/desconhecido/versao futura."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        state = data.get("state") if isinstance(data, dict) else None
        version = data.get("version") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return "ambiguous"
    if version != RECEIPT_VERSION or state not in BLOCKING + RETRYABLE:
        return "ambiguous"
    return state


def _persist(core, path, receipt):
    core.write_json_atomic(path, receipt)
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_expected(info, expected_pane, expected_terminal):
    if ((expected_pane is not None and info.get("pane_id") != expected_pane)
            or (expected_terminal is not None and info.get("terminal_id") != expected_terminal)):
        raise RuntimeError("o nome resolve para outro pane/terminal que o esperado; nada enviado")


def ensure_session(name, root, slug, siblings, core, review_groups, *,
                   timeout_s=TIMEOUT_S, sleep=time.sleep, monotonic=time.monotonic,
                   expected_pane=None, expected_terminal=None):
    """Devolve o info validado. Sem efeito se o revisor já tem sessão ou não é o caso.

    Roda sob a reserva de submissão do core (lock por agent + lock físico por pane), o mesmo
    que o dispatch normal usa: dois dispatchers não passam juntos pela janela do envio."""
    info = core.validate_agent_project(name, root)
    _check_expected(info, expected_pane, expected_terminal)  # antes de QUALQUER retorno antecipado
    if not needs_handshake(info):
        return info
    if review_groups.group_for_agent(name) is not None:
        raise RuntimeError("sessão do revisor não observável; papel de grupo usa a hidratação inicial")
    with core._dispatch_submission_locks({name: info}):
        locked = core.validate_agent_project(name, root)  # reobserva sob a reserva
        if core._dispatch_differences(info, locked, include_lifecycle=False):
            raise RuntimeError("o alvo mudou entre a leitura e a reserva; nada enviado")
        info = locked
        # Vinculo vindo do chamador (ex.: herdr-agent-restart acabou de religar ESTE pane): um nome que
        # passou a resolver para outro pane/terminal nunca recebe o prompt.
        _check_expected(info, expected_pane, expected_terminal)
        if not needs_handshake(info):
            return info
        pane = info["pane_id"]
        proc = process_key(info, core, root)
        path = receipt_path(root, name, info, proc)
        # Formatos anteriores continuam consultados: atualizar o codigo nunca autoriza reenvio.
        # r182 (por terminal) nao tem vinculo fisico: qualquer estado que possa ter enviado,
        # `complete` inclusive, bloqueia (perder a metadata da sessao nao prova troca de processo).
        # r183 (terminal+pid+starttime) tem vinculo: mesma encarnacao bloqueia pelo estado.
        for label, old_path in (("r182", legacy_receipt_path(root, name, info)),
                                ("r183", intermediate_receipt_path(root, name, info, proc))):
            old = _prior_state(old_path)
            if old is not None and old not in RETRYABLE:
                raise RuntimeError(f"recibo de formato anterior ({label}) em estado {old}; não reenviar sem "
                                   f"diagnóstico. Depois de conferir o pane à mão, apague {old_path}")
        prior = _prior_state(path)
        if prior is not None and prior not in RETRYABLE:
            raise RuntimeError(
                f"primeiro turno já tentado para este processo (estado {prior}); não reenviar. "
                f"Depois de conferir o pane à mão, apague {path} para liberar uma nova tentativa")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        receipt = dict(version=1, operation_id=str(uuid.uuid4()), state="prepared",
                       created_at=core.now_iso(), name=name, pane_id=pane,
                       terminal_id=info["terminal_id"], process=proc)
        submitted = False
        try:
            core.require_can_send(name, expected_pane=pane)
            diff = core._dispatch_differences(info, core.get_agent_info(name), include_lifecycle=False)
            if diff:
                raise RuntimeError("identidade mudou antes do envio: " + "; ".join(diff))
            if process_key(info, core, root) != proc:
                raise RuntimeError("processo Codex mudou antes do envio")
            prompt = core.role_reinforcement_prompt(name, slug, root, siblings)
            receipt["state"] = "delivery_attempted"
            _persist(core, path, receipt)  # duravel ANTES do subprocesso
            # Guarda de novo logo depois do I/O duravel, imediatamente antes de enviar.
            try:
                core.require_can_send(name, expected_pane=pane)
                if core._dispatch_differences(info, core.get_agent_info(name), include_lifecycle=False):
                    raise RuntimeError("identidade mudou depois do recibo")
                if process_key(info, core, root) != proc:
                    raise RuntimeError("processo Codex mudou depois do recibo")
            except RuntimeError:
                receipt.update(state="aborted_before_send", error="guarda/identidade recusou depois do recibo")
                _persist(core, path, receipt)
                raise
            submitted = True
            try:
                result = subprocess.run(
                    core.herdr_argv("agent", "prompt", pane, prompt, "--wait", "--until", "working",
                                    "--timeout", "30000"),
                    capture_output=True, text=True, timeout=core.CLI_TIMEOUT_S)
                if result.returncode:
                    receipt.update(state="delivery_unconfirmed", delivery_error=f"exit_{result.returncode}")
                    _persist(core, path, receipt)
            except (OSError, subprocess.TimeoutExpired) as exc:
                receipt.update(state="delivery_unconfirmed", delivery_error=type(exc).__name__)
                _persist(core, path, receipt)
            # Só observa daqui em diante: nem erro nem timeout autorizam um segundo envio.
            deadline = monotonic() + timeout_s
            while True:
                current = core.get_agent_info(name)
                if (current.get("pane_id") != pane or current.get("terminal_id") != info["terminal_id"]
                        or current.get("agent") != "codex"):
                    raise RuntimeError("identidade do pane mudou durante o primeiro turno")
                if _native_session(current) and process_key(info, core, root) != proc:
                    raise RuntimeError("processo Codex mudou durante o primeiro turno; sessão não atribuída")
                if (_native_session(current) and current.get("interactive_ready") is True
                        and core.effective_status(current) in ("idle", "done")):
                    receipt.update(state="complete", completed_at=core.now_iso(),
                                   native_session=current["agent_session"])
                    _persist(core, path, receipt)
                    return core.validate_agent_project(name, root)
                if current.get("agent_status") == "blocked" or monotonic() >= deadline:
                    raise RuntimeError("primeiro turno sem sessão nativa assentada; não reenviar")
                sleep(0.5)
        except (RuntimeError, OSError, ValueError) as exc:
            if receipt["state"] != "aborted_before_send":
                receipt.update(state="ambiguous" if submitted else "aborted_before_send",
                               error=str(exc), observed_at=core.now_iso())
                if submitted:
                    _persist(core, path, receipt)
            raise RuntimeError(f"primeiro turno do revisor incompleto: {exc}") from exc


def select_level1_candidates(names, root, core, review_groups, review_candidate):
    """Ordem de preferencia do nivel 1: pronto, depois Codex reciclado (elegivel ao primeiro turno,
    que roda so para o escolhido), depois ausente recuperavel. Nao envia nada."""
    ready, recyclable, missing = [], [], []
    for name in names:
        try:
            probe = core.validate_agent_project(name, root)
            if needs_handshake(probe) and review_groups.group_for_agent(name) is None:
                recyclable.append(name)
                continue
            info = review_candidate(name, root)
            if core.effective_status(info) in ("idle", "done"):
                ready.append(name)
        except RuntimeError as exc:
            if core._is_agent_not_found(exc):
                missing.append(name)
            # Um pane existente que diverge nunca e' candidato.
    return ready or recyclable or missing


ROLES = ("exec", "rev-1", "rev-2", "rev", "scout")
# Quem pode rodar o handshake contra agent de OUTRO space (decisao do coordenador, 18/11 12:20).
CROSS_SPACE_CALLERS = ("herdr-exec", "coordenador")


def split_name(name):
    for role in sorted(ROLES, key=len, reverse=True):
        if name.endswith("-" + role):
            return name[: -len(role) - 1], role
    return None, None


def authorize_caller(target_name, caller_name):
    """Devolve None se permitido, ou o motivo da recusa. Caller nao comprovado nunca e' aceito."""
    if not caller_name:
        return "quem roda não foi comprovado (sem pane Herdr/prova nativa); recusado"
    if caller_name in CROSS_SPACE_CALLERS:
        return None
    target_slug, _ = split_name(target_name)
    caller_slug, _ = split_name(caller_name)
    if target_slug is not None and caller_slug == target_slug:
        return None
    return (f"'{caller_name}' é de outro space que '{target_name}'; só o próprio space, "
            f"{' e '.join(CROSS_SPACE_CALLERS)} podem rodar isto")


def after_restart(name, pane, terminal_id, core, review_groups, *, expected_cwd=None,
                  quiet_timeout_s=90, timeout_s=150):
    """Chamado pelo herdr-agent-restart depois de reiniciar um Codex: se ele voltou sem sessao,
    faz o primeiro turno agora (a reciclagem ja sai com sessao). `pane` e `terminal_id` sao os do
    agent que o restart acabou de religar e validar; um nome que resolva para outro lugar NAO e'
    hidratado (achado r191 rev-1/rev-2). Devolve (status, detalhe) e nunca levanta: a reciclagem ja
    foi feita e nao pode virar falha por causa do handshake."""
    try:
        if not pane or not terminal_id:
            return "skipped", "pane/terminal da reciclagem desconhecidos; nada enviado"
        info = core.get_agent_info(name)
        if info.get("pane_id") != pane or info.get("terminal_id") != terminal_id:
            return "failed", "nome resolve para outro pane/terminal; nada enviado"
        if info.get("agent_session") is not None:
            return "has_session", None
        slug, _ = split_name(name)
        if slug is None:
            return "skipped", "nome sem papel conhecido"
        root = core.project_root(info.get("cwd") or "")
        if expected_cwd is not None and root != core.project_root(expected_cwd):
            return "failed", "o projeto do agent mudou depois do restart; nada enviado"
        # o Codex recem-religado ainda pode estar no turno de boot
        core.wait_quiescent(name, timeout_s=quiet_timeout_s)
        siblings = [f"{slug}-{r}" for r in ("exec", "rev-1", "rev-2") if f"{slug}-{r}" != name]
        done = ensure_session(name, root, slug, siblings, core, review_groups, timeout_s=timeout_s,
                              expected_pane=pane, expected_terminal=terminal_id)
        if done.get("pane_id") != pane or done.get("terminal_id") != terminal_id:
            return "failed", "o resultado não corresponde ao pane/terminal da reciclagem"
        return "done", (done.get("agent_session") or {}).get("value")
    except Exception as exc:  # noqa: BLE001
        return "failed", str(exc)[:300]
