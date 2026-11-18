"""Quem pode rodar herdr-agent-restart (decisao do coordenador, 18/11 12:40; r194 rev-1).

Mesma regra do herdr-codex-handshake: agent do proprio space (prefixo do nome E mesmo projeto) com prova
nativa, herdr-exec ou coordenador. Sem prova = recusa. Reinicio do PROPRIO pane: o pai inicia um helper
(--_separado) e FICA VIVO ate o helper provar nativamente a mesma cadeia e a autorizacao e responder READY
por um pipe; so entao o pai retorna e o helper segue sozinho (prova mantida em memoria, alvo revalidado
depois do atraso). Nenhum arquivo e' fonte de autoridade."""
import os
import select
import sys


def authorize(name, info, caller_pane, core, hs):
    """None se permitido; senao o motivo da recusa."""
    if not caller_pane:
        return hs.authorize_caller(name, None)
    try:
        caller = core.get_agent_info(caller_pane)
    except Exception:  # noqa: BLE001 - sem leitura = nao comprovado
        return hs.authorize_caller(name, None)
    caller_name = caller.get("name")
    refusal = hs.authorize_caller(name, caller_name)
    if refusal:
        return refusal
    if caller_name not in hs.CROSS_SPACE_CALLERS:
        try:
            same = (core.project_root(caller.get("cwd") or "")
                    == core.project_root(info.get("foreground_cwd") or info.get("cwd") or ""))
        except Exception:  # noqa: BLE001
            same = False
        if not same:
            return f"o projeto de '{caller_name}' não é o do alvo"
    return None


def identity(info):
    """Identidade comparavel de um agent: quem e', onde esta e o que roda. Status nao entra."""
    info = info or {}
    return (info.get("name"), info.get("pane_id"), info.get("terminal_id"), info.get("agent"),
            (info.get("agent_session") or {}).get("value"), info.get("cwd"), info.get("foreground_cwd"))


def caller_snapshot(core, caller_pane):
    try:
        return identity(core.get_agent_info(caller_pane))
    except Exception:  # noqa: BLE001
        return None


def signal_ready():
    """Helper (--_separado): a autorizacao ja foi provada com o pai vivo; avisa o pai e solta o terminal dele."""
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 1)
    os.dup2(null, 2)


def wait_ready(proc, timeout_s=30, select_=select.select):
    """Pai: espera o helper confirmar. Devolve (ok, linha): ok so com a linha READY; a recusa do helper
    (stderr junto no pipe) vem em `linha` para o pai mostrar; saida sem linha ou prazo = (False, '')."""
    try:
        ready, _, _ = select_([proc.stdout], [], [], timeout_s)
        if not ready:
            return False, ""
        line = proc.stdout.readline().decode("utf-8", "replace").strip()
        return line == "READY", line
    except (OSError, ValueError):
        return False, ""
