"""Idle do Codex inferido pela tela -- regra unica do mesh.

Herdr 0.9.2 (#4563, refs #4507) parou de DEDUZIR idle do Codex pela tela: um
titulo, a caixa de composicao ou a falta de spinner "nao estabelece que o turno
acabou", porque o Codex saia como idle no meio de um turno (saida em frases, sem
spinner) e gerava "done" falso. Resultado: todo Codex fica `unknown` depois de um
turno. O idle por hook (#4756) exige `codex --no-daemon` + hook v9 e nao saiu em
release (nem 0.9.3). Ate la o mesh INFERE, sem mexer no manifesto do Herdr
(mexer traria os "done" falsos de volta pro notify-watch, pro Heeler e pra
qualquer `--wait`).

Usada por: herdr-agents (exibe `idle?`), herdr-ask, herdr-review-dispatch e o
preflight de _herdr_dispatch (tratam como idle; autorizado pelo Breno em
2026-09-29 23:42). Na duvida NAO e' ocioso: falso "ocupado" e' barato, falso
"ocioso" e' o que o upstream removeu.
"""
import re

CAIXA_VAZIA = re.compile(r"Ask Codex to do anything")
# Timer vivo entre parenteses ("Working (12s • esc to interrupt)") ou a dica de
# interrupcao. O braille que o Codex anima sobre a linha da caixa NAO conta: ele
# aparece tambem com o agent parado (achado de 2026-09-22).
TRABALHANDO = re.compile(r"to interrupt|\((?:\d+[hm] )*\d+s(?: [•·][^)\r\n]*)?\)")


def codex_ocioso_pela_tela(texto):
    """True so se a ULTIMA caixa de composicao esta vazia e nao ha marcador de
    trabalho no recorte. Texto digitado na caixa (rascunho humano) tira o
    placeholder e, por isso, nao conta como ocioso."""
    if not texto:
        return False
    linhas = [l for l in texto.splitlines() if l.strip()]
    recorte = "\n".join(linhas[-12:])
    return CAIXA_VAZIA.search(recorte) is not None and not TRABALHANDO.search(recorte)


def inferir_idle(info, ler_tela):
    """True se `info` (um agent do Herdr) e' um Codex `unknown`, pronto para
    entrada, com a tela ociosa. `ler_tela(info) -> str` fica por conta do
    chamador (cada script le o pane do seu jeito)."""
    if not (info.get("agent_status") == "unknown" and info.get("agent") == "codex"
            and info.get("interactive_ready")):
        return False
    try:
        return codex_ocioso_pela_tela(ler_tela(info))
    except Exception:  # falha de leitura nunca vira "ocioso"
        return False
