"""Gera handoff a partir do log da sessao, usando uma LLM local (Ollama).

Existe pro caso em que o agent que sai NAO CONSEGUE escrever o proprio resumo --
limite de uso esgotado, travado, sem credito. Ate agora a unica saida era
`--sem-handoff`, que entrega o log cru ao sucessor e perde o resumo inteiro.
Aqui a LLM local le esse mesmo log e escreve o resumo, sem custo de conta.

MEDIDO (2026-09-12, RTX 5090, qwen3-coder:30b, sessao real do llm-exec):
prompt de 95.868 chars -> prompt_eval_count 30.276, sem truncamento; saida com
zero identificadores inventados, e ainda derivou corretamente um valor
(max_input_chars=60000) que nao estava na transcricao.

A MESMA medicao com o prompt grande demais produziu o oposto: numeros de rodada
fabricados e a afirmacao de ter rodado um script que o projeto proibe. A
diferenca entre os dois resultados NAO foi o modelo, foi o truncamento
silencioso. Dai as tres guardas abaixo serem obrigatorias, nao opcionais.
"""

import json
import os
import re as _re
import urllib.error
import urllib.request

# Medido na rodada limpa: 95.868 chars / 30.276 tokens = 3,17. Uso 3,0 pra
# arredondar contra mim -- superestimar tokens corta mais log do que o
# necessario (perda de contexto), subestimar TRUNCA (perda silenciosa). As duas
# sao ruins, mas so uma avisa.
CHARS_POR_TOKEN = 3.0

# Fracao do teto por requisicao que aceito ocupar. O resto e' folga pro
# preambulo, pro template e pra variacao de tokenizacao entre conteudos.
FOLGA = 0.75

OLLAMA_URL = os.environ.get("HERDR_OLLAMA_URL", "http://127.0.0.1:11434")
MODELO = os.environ.get("HERDR_OLLAMA_MODEL", "qwen3-coder:30b")

# O teto POR REQUISICAO, nao o num_ctx pedido. Sob NUM_PARALLEL=2 o servidor
# corta em ~49.152 mesmo declarando 98.304 -- mecanismo ainda em disputa
# (hipoteses A/B, ver RESULTADO-fase6b no llm-bench). Por isso o numero aqui e'
# o CORTE OBSERVADO, nao o calculado: nao depende de qual hipotese vence.
TETO_POR_REQUISICAO = int(os.environ.get("HERDR_OLLAMA_TETO", "49152"))

# 2.048 cortou a saida no meio da frase na medicao. O handoff do Claude na mesma
# sessao tinha 10.521 chars -- a ~3 chars/token, ~3.500 tokens.
NUM_PREDICT = 6144


class LocalIndisponivel(RuntimeError):
    """Ollama fora do ar, modelo ausente, ou resposta sem o campo esperado."""


class Truncou(RuntimeError):
    """O servidor avaliou menos tokens do que mandamos.

    NAO e' um aviso: e' erro. Um handoff gerado a partir de metade do log sai
    fluente, bem estruturado, com nomes de arquivo reais -- e com
    identificadores inventados no lugar dos que o modelo nao viu. Degrada a
    ancoragem factual, nao a fluencia, entao passa em revisao superficial.
    Falhar alto e' a unica forma de isso nao virar um handoff silenciosamente
    errado.
    """


INSTRUCAO = """Abaixo está a transcrição de uma sessão de trabalho de um agent de engenharia no projeto {projeto}. Escreva o HANDOFF dessa sessão: o documento que a próxima pessoa a assumir o projeto vai ler.

Cubra, nesta ordem:
- o que estava sendo feito e por quê
- decisões já tomadas (e o porquê, se não for óbvio)
- o que falta / próximo passo
- arquivos relevantes e qualquer coisa que quem assumir precise saber pra não repetir trabalho ou perder contexto

Escreva só o handoff, em português, em Markdown. Não escreva perguntas de verificação, não escreva gabaritos, não comente sobre a tarefa.

Cite identificadores reais (hashes de commit, nomes de arquivo, números) APENAS quando eles aparecerem na transcrição. Nunca invente um identificador que você não viu. Se não souber, escreva que não sabe.

--- TRANSCRIÇÃO ---
"""

AVISO_CABECALHO = """> **Handoff gerado por LLM local ({modelo}), não pelo agent que saiu.**
> Ele não conseguiu escrever o próprio resumo, então este texto foi extraído do
> log da sessão em `{log}`.
>
> Duas consequências práticas: **confira todo hash de commit e todo caminho de
> arquivo antes de agir sobre ele**, e trate as decisões aqui como resumo de
> terceiro, não como palavra de quem decidiu. O log completo continua no
> caminho acima e é a fonte, não este arquivo.
>
> Não há arquivo de verificação para este handoff — só o agent que viveu a
> sessão sabe o que deixou de fora, e ele não estava disponível.

"""


def mensagens_do_log(caminho):
    """Extrai (papel, texto) das mensagens user/assistant de um .jsonl."""
    msgs = []
    with open(caminho, encoding="utf-8", errors="ignore") as fh:
        for linha in fh:
            try:
                d = json.loads(linha)
            except ValueError:
                continue
            m = d.get("message")
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                continue
            c = m.get("content")
            partes = []
            if isinstance(c, str):
                partes.append(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        partes.append(b.get("text", ""))
            txt = "\n".join(p for p in partes if p).strip()
            if txt:
                msgs.append(f"[{m['role']}] {txt}")
    return msgs


def orcamento_chars(teto_tokens=None, folga=FOLGA, chars_por_token=CHARS_POR_TOKEN):
    """Quantos chars de log cabem numa requisicao."""
    teto = TETO_POR_REQUISICAO if teto_tokens is None else teto_tokens
    return int(teto * folga * chars_por_token)


def recorta(msgs, limite_chars):
    """Fica com as mensagens MAIS RECENTES que couberem.

    Pelo fim, nao pelo comeco: o estado atual do trabalho e o que o sucessor
    precisa, e o inicio da sessao costuma ser preambulo.
    """
    acc, tot = [], 0
    for m in reversed(msgs):
        if tot + len(m) + 2 > limite_chars:
            break
        acc.append(m)
        tot += len(m) + 2
    acc.reverse()
    return acc


# O modelo local IGNORA a instrucao de nao escrever verificacao. Medido duas
# vezes (2026-09-12, qwen3-coder:30b) com a proibicao explicita no prompt: nas
# duas ele emendou "## POSITIVAS / ## NEGATIVAS" com GABARITOs no fim do
# handoff. A causa e' plausivel e nao se corrige pedindo de novo -- o proprio
# HANDOFF_PROMPT do herdr-swap aparece DENTRO da transcricao que ele esta
# lendo, entao ele imita a forma que ve em vez de seguir a que pedimos.
#
# Cortar no codigo em vez de insistir no prompt. E o corte importa por
# seguranca, nao por estetica: um GABARITO escrito por quem NAO viveu a sessao
# e' uma afirmacao inventada com cara de fonte autoritativa -- exatamente o
# formato que ninguem relê. Numa das rodadas ele "gabaritou" ter executado dez
# vezes um script que o projeto proibe rodar na maquina.
CABECALHOS_DE_VERIFICACAO = _re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*)?\s*(?:POSITIVAS|NEGATIVAS|"
    r"TESTE\s+DE\s+VERIFICA\u00c7\u00c3O|VERIFICA\u00c7\u00c3O)\b",
    _re.I | _re.M)


def corta_verificacao(texto):
    """Devolve (texto_limpo, cortou_bool).

    Corta a partir do PRIMEIRO cabecalho de verificacao ate o fim -- o modelo
    sempre a emenda no final, depois do handoff de verdade.
    """
    m = CABECALHOS_DE_VERIFICACAO.search(texto)
    if not m:
        return texto, False
    return texto[:m.start()].rstrip() + "\n", True


def _post(url, payload, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def gerar(log_path, projeto, modelo=MODELO, url=OLLAMA_URL, timeout=900):
    """Devolve (texto_do_handoff, metricas). Levanta Truncou se cortou.

    O `num_ctx` declarado e' o dobro do teto observado de proposito: sob
    NUM_PARALLEL=2 o servidor entrega metade do declarado, e sob =1 entrega
    tudo. Declarar o dobro faz a requisicao caber nas DUAS hipoteses, sem
    depender de saber qual e' a certa.
    """
    msgs = mensagens_do_log(log_path)
    if not msgs:
        raise LocalIndisponivel(f"nenhuma mensagem legivel em {log_path}")

    usadas = recorta(msgs, orcamento_chars())
    if not usadas:
        raise LocalIndisponivel(
            "nem a mensagem mais recente do log cabe no orcamento -- "
            f"teto {TETO_POR_REQUISICAO} tokens")

    corpo = "\n\n".join(usadas)
    prompt = (INSTRUCAO.format(projeto=projeto) + corpo
              + "\n--- FIM DA TRANSCRIÇÃO ---\n\nEscreva o handoff agora.")

    try:
        d = _post(url + "/api/generate", {
            "model": modelo,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_ctx": TETO_POR_REQUISICAO * 2,
                "temperature": 0.2,
                "num_predict": NUM_PREDICT,
            },
        }, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LocalIndisponivel(f"{url}: {exc}") from exc

    texto = (d.get("response") or "").strip()
    if not texto:
        raise LocalIndisponivel(f"{modelo} respondeu vazio")
    texto, cortou = corta_verificacao(texto)
    if not texto.strip():
        raise LocalIndisponivel(
            f"{modelo} devolveu SO verificacao, sem handoff -- nada aproveitavel")

    metricas = {
        "prompt_chars": len(prompt),
        "prompt_eval_count": d.get("prompt_eval_count"),
        "eval_count": d.get("eval_count"),
        "msgs_no_log": len(msgs),
        "msgs_usadas": len(usadas),
        "segundos": round(d.get("total_duration", 0) / 1e9, 1),
        "cortou_verificacao": cortou,
    }
    confere_truncamento(metricas)
    return texto, metricas


def confere_truncamento(metricas, chars_por_token=CHARS_POR_TOKEN):
    """CONFERIR, nao so DECLARAR -- ver lacuna 9 do llm-gateway.

    Nao estimo tokens a partir de chars pra comparar com o enviado: isso vira
    limiar heuristico sobre chars/token, a familia de erro que ja falhou aqui.
    A conferencia e' contra o TETO, que e' um numero conhecido: se o servidor
    avaliou um valor colado no teto, ele parou no teto, nao no fim do prompt.
    """
    pe = metricas.get("prompt_eval_count")
    if not pe:
        # Sem o contador nao da pra afirmar que passou. Nao afirmo.
        metricas["truncamento"] = "indeterminado (servidor nao reportou prompt_eval_count)"
        return metricas
    esperado = metricas["prompt_chars"] / chars_por_token
    if pe >= TETO_POR_REQUISICAO * 0.98:
        raise Truncou(
            f"prompt_eval_count={pe:,} colado no teto de {TETO_POR_REQUISICAO:,} "
            f"(mandei ~{esperado:,.0f} tokens) -- o prompt foi cortado e o "
            f"handoff sairia com identificadores inventados. Reduza "
            f"HERDR_OLLAMA_TETO ou use um modelo com mais contexto.")
    metricas["truncamento"] = "nao"
    return metricas
