"""Cache das leituras de model/effort feitas ao vivo nos panes.

Por que existe: as tres fontes que o `herdr-agents` ja consulta desatualizam de
formas diferentes, e nenhuma cobre o caso mais comum -- alguem trocar o modelo
com `/model` num pane vivo.

  argv    congela no lancamento. Um `/model` depois nao aparece nunca.
  thread  (log da sessao) so registra o modelo quando o agent RESPONDE. Entre a
          troca e a proxima resposta, o log mostra o valor antigo -- e o
          `herdr-agents` acerta ao recusar afirmar, virando `(unknown)`.
  global  e' o default do config, reescrito pelo proprio `/model`.

A leitura do pane e' a unica que ve o estado atual, e para Codex ela e'
autoritativa: o rodape imprime `gpt-6-astra medium` e o proprio TUI escreve
`Model changed to ...` quando muda. Para Claude nao ha nada equivalente na tela.

O ganho de amostrar DE TEMPOS EM TEMPOS, e nao so na hora de listar:

  Codex   pega a troca assim que ela acontece, sem custo nenhum -- `agent read`
          nao toca no pane, so le o buffer.
  Claude  a heuristica do log e' uma CORRIDA: o log so vira confiavel quando
          chega uma resposta depois da troca. Uma leitura unica perde essa
          janela e o agent fica `(unknown)` pra sempre. Amostrando repetido, a
          primeira amostra que cair depois de uma resposta CONFIRMA o valor --
          e o cache guarda essa confirmacao mesmo que a proxima troca volte a
          embaralhar o log.

O cache nunca inventa: guarda valor, de onde veio e QUANDO. Quem le decide se
uma confirmacao de 40 minutos atras ainda serve.
"""

import datetime
import json
import os
import tempfile

CACHE_PATH = os.environ.get(
    "HERDR_PROBE_CACHE",
    os.path.join(os.environ.get("XDG_STATE_HOME",
                                os.path.expanduser("~/.local/state")),
                 "herdr-mesh-tools", "probe.json"))

# Ordem de confianca. Uma leitura pior NUNCA sobrescreve uma melhor -- senao a
# proxima passagem da sonda apagaria uma confirmacao boa com um palpite.
CONFIANCA = {"pane": 3, "thread-confirmado": 2, "thread": 1}


def agora():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def ler(path=None):
    try:
        with open(path or CACHE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def gravar(dados, path=None):
    """Escrita atomica: tmp no mesmo diretorio + os.replace().

    A sonda roda por timer enquanto alguem pode estar listando. Um `open(w)`
    direto deixa janela pra leitura pegar arquivo pela metade.
    """
    p = path or CACHE_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dados, fh, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def melhor(anterior, novo):
    """Qual das duas leituras fica. Empate de confianca -> a mais recente."""
    if not anterior:
        return novo
    if not novo:
        return anterior
    ca = CONFIANCA.get(anterior.get("origem"), 0)
    cn = CONFIANCA.get(novo.get("origem"), 0)
    if cn > ca:
        return novo
    if cn < ca:
        return anterior
    return novo if novo.get("visto_em", "") >= anterior.get("visto_em", "") else anterior


def idade_min(entrada, ref=None):
    """Minutos desde a leitura. None se nao der pra saber."""
    try:
        t = datetime.datetime.fromisoformat(entrada["visto_em"])
    except (KeyError, TypeError, ValueError):
        return None
    ref = ref or datetime.datetime.now(datetime.timezone.utc)
    return int((ref - t).total_seconds() // 60)


def atualiza(cache, nome, model, effort, origem, kind=None, quando=None):
    """Registra uma leitura, respeitando a ordem de confianca."""
    novo = {"model": model, "effort": effort, "origem": origem,
            "visto_em": quando or agora()}
    if kind:
        novo["kind"] = kind
    cache[nome] = melhor(cache.get(nome), novo)
    return cache
