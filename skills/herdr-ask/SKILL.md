---
name: herdr-ask
description: "Dispara uma consulta de design cega e paralela pros dois consultores (<slug>-rev-1, <slug>-rev-2) de um space do Herdr, ou a terceira análise automática do <slug>-scout quando o exec julgar necessário após duas rodadas. Use para uma pergunta de design ABERTA — não uma revisão de código. Requer HERDR_ENV=1 e rodar dentro de um pane cujo agent se chama <slug>-exec."
---

# herdr-ask

Mesma mecânica cega/paralela do `herdr-review` (isolamento por diretório,
congelamento de contexto, os alvos assentando antes de você ler qualquer
resposta), mas pra pergunta de design aberta em vez de revisão de código já
feito. Não é bug hunt: não há achado atômico, não há `APPROVE`, não há
CONFIRMADO/ÚNICO/CONFLITO — a resposta é uma **posição com premissas
explícitas**, e é isso que torna duas respostas divergentes reconciliáveis.

Antes de qualquer coisa, confirme que está num pane do Herdr:

```bash
test "${HERDR_ENV:-}" = 1
```

`$HERDR_ENV` vazio não é prova de estar fora do Herdr se você é um CLI que
roda comandos via sandbox (confirmado 2026-09-01 com Codex: o sandbox de
execução do `codex-code-mode-host` esconde toda variável `HERDR_*`, mesmo o
processo do agent tendo-a de verdade). Se o check acima falhar, confirme com
`herdr agent list` antes de concluir qualquer coisa — ver a skill `herdr`
para o achado completo e o critério de quando realmente concluir que está
fora do Herdr.

## Quando usar

Antes de construir algo com mais de uma abordagem plausível, ou antes de
decidir entre trade-offs reais (arquitetura, design de API, se vale a pena
extrair algo, como sequenciar um trabalho arriscado) — não depois que o
código já existe (aí é revisão, use `herdr-review`). Sinal de que vale a
pena: você mesmo não tem uma posição forte, ou tem uma posição mas sabe que
está calculando parcialmente por trás.

Não use pra decisão trivial com resposta óbvia — o custo de uma rodada
(dois consultores, potencialmente minutos) só compensa quando a pergunta
realmente tem mais de uma resposta defensável.

## Descobrir seu próprio slug

Mesmo mecanismo do `herdr-review` — resolva via `HERDR_PANE_ID`:

```bash
slug=$(herdr agent list | python3 -c "
import json, sys, os
data = json.load(sys.stdin)
pane = os.environ['HERDR_PANE_ID']
for a in data['result']['agents']:
    if a.get('pane_id') == pane and a.get('name', '').endswith('-exec'):
        print(a['name'][:-5]); break
")
```

## Disparar a consulta

Escreva a pergunta num arquivo (markdown, pode ser longa e ter seções) —
`--question-file`, não texto inline: uma pergunta de design de verdade não
cabe num argv legível.

```bash
herdr-ask "$slug" --question-file /caminho/pergunta.md
```

Isso cria `.herdr/ask/<slug>-<n>/` (namespace **separado** de
`.herdr/review/` — não compartilha numeração de rodada), uma pasta por
consultor dentro dela, escreve o `request.md` de cada um (protocolo de
consulta + `.herdr/reviewer.md` do projeto, se existir, + sua pergunta),
dispara `<slug>-rev-1` e `<slug>-rev-2` em paralelo, cegos um do outro, espera
os dois assentarem. O dispatcher entrega a consulta aos panes existentes sem reset automático.
Model e `reasoning_effort` podem ser capturados como evidência, mas não são
permissão de despacho: diferenças, mudanças ou valores desconhecidos não
abortam a consulta. Quando for necessária uma fronteira limpa, recrie o pane
explicitamente com `herdr-swap`, copiando o handoff; para acompanhamento
rotineiro use `herdr-context-watch`, que é somente leitura. Reasoning não é
dimensão de seleção e nenhum default é inventado.

O lifecycle do Herdr não acompanha a escrita do turno: `agent prompt --wait`
pode devolver `done` antes de o consultor publicar `answer.md`. Por isso o
dispatcher exige, além de `idle`/`done`, um `answer.md` regular, não vazio e
novo (ou alterado) desde o disparo. Se o prazo terminar sem esse artefato, o
resultado é `artifact_missing` e a consulta falha; nunca trate `done` com
`answer_bytes: 0` como resposta válida. O `metrics.json` é publicado por um
temporário no mesmo diretório e `os.replace`, de modo que leituras concorrentes
não observem JSON parcial.

Os panes de `rev-1`, `rev-2` e `scout` são headless. Uma composição residual
(inclusive uma letra digitada por engano) é descartável e não impede o reset ou
o próximo despacho; somente um diálogo real do CLI, reportado pelo canal
oficial como `agent_blocked`, interrompe a cadeia. A proteção contra texto
humano não enviado continua valendo para o `-exec` e para o `herdr-swap`.

Antes de criar os prompts, o dispatcher revalida sob lock curto o snapshot
capturado de cada alvo: status, pane, workspace, cwd, sessão, revision e
`state_change_seq`, além da ausência de diálogo. Qualquer mudança aborta sem
enviar e fica registrada em `preflight_error`. Se `agent_prompt --wait` retornar
`agent_prompt_stalled`, a mesma checagem ocorre antes do único retry; um alvo já
`working`, ou cuja sequência avançou, recebe `agent wait` e não um segundo
prompt. O reenvio só ocorre com a identidade e o estado idle/done inalterados.

Precisa que os dois leiam material além da pergunta em si (código real,
outro doc)? `--context <path> [<path> ...]` congela cada arquivo no disparo
(mesma lógica do `--files` do `herdr-review-dispatch` — sem isso, o material
pode mudar no meio da consulta e a resposta sai sobre algo que já não existe
mais). Pergunta barata, só precisa de uma opinião? `--reviewer rev` ou
`--reviewer rev-2` consulta um só, mais barato — mas perde a comparação
entre posições independentes, que é o ponto principal disto.

`--timeout <segundos>` muda o teto de espera por consultor (default 1200).
Sai com código 0 só se todos os consultores despachados produziram resposta
não vazia.

Se um consultor obrigatório não estiver registrado (`agent_not_found`), o
dispatcher tenta automaticamente `herdr-bootstrap --slug <slug>` antes de
criar a consulta e só prossegue depois de observar o agent restaurado em
`idle`/`done`. A tabela fornece perfil apenas quando não existe pane/base para
reaproveitar; um pane já em uso conserva seu modelo e raciocínio durante
limpeza, restauração ou swap. Falha real de recuperação é registrada como
bloqueio operacional, sem enviar uma consulta parcial.

## Terceira análise automática pelo scout

Quando `herdr-review` encontrar uma disputa que o exec não consiga resolver,
ou quando duas rodadas de correção terminarem com um achado aberto, o exec
pode julgar necessária uma terceira análise. Nesse caso, escreva uma pergunta
focada e execute automaticamente:

```bash
herdr-ask "$slug" --question-file /caminho/da-duvida.md \
  --reviewer scout --context /caminho/material-relevante
```

O `--reviewer scout` envia para `<slug>-scout` sem reset implícito. Essa
chamada é a terceira análise automática, não uma terceira rodada de correção
dos revisores. O scout escreve `answer.md` para o
`<slug>-exec`; não fala diretamente com o usuário, não decide pelo space e não
faz commit. Se continuar incerto ou devolver uma divergência, declara isso
explicitamente para o exec. Depois de ler a resposta, se o exec ainda julgar
necessária outra análise, ou se houver INCERTEZA/DIVERGÊNCIA, somente ele leva a
questão ao Breno.

## Ler e reconciliar

Leia os `answer.md` de cada consultor (`<round_dir>/<slug>-rev-1/answer.md`,
`<round_dir>/<slug>-rev-2/answer.md`) — cada resposta tem: a decisão numa
frase, as premissas explícitas que a sustentam, o argumento, uma recomendação
executável, a validação e o próximo passo. `ação necessária: nenhuma` ou
`recomendação: não determinada`, com a lacuna e a pergunta explícitas, também
são respostas completas.

O `<slug>-exec` deve devolver ao Breno uma síntese com posição, recomendação,
decisão do exec, próximo passo e critério de conclusão. Não basta repetir a
posição ou obrigar o usuário a perguntar qual é a solução.

**Não trate isso como revisão de código.** A reconciliação certa depende das
premissas, não só da conclusão:

| Conclusões | Premissas | O que é | O que fazer |
|---|---|---|---|
| iguais | iguais | convergência real | siga com confiança |
| opostas | a mesma premissa carregando o peso | discordância genuína | acione o scout; se continuar, somente o exec leva ao Breno |
| opostas | diferentes | não é conflito — é uma questão de fato em aberto | resolva você mesmo descobrindo qual premissa vale (não precisa escalar) |

A terceira linha é a mais fácil de errar: duas conclusões opostas parecem
CONFLITO à primeira vista, mas se cada consultor está certo *dado o que
assumiu*, o trabalho é verificar qual premissa é verdadeira, não arbitrar
entre posições.

## O que não fazer

- Não force um formato de achado atômico (P0-P3, arquivo:linha) na resposta
  — se um consultor responder assim mesmo tendo o protocolo pedido posição +
  premissas, não é erro dele, mas não trate como se fosse mais rigoroso só
  por parecer uma lista.
- Não decida sozinho uma discordância genuína (mesma premissa, conclusão
  oposta) — acione a terceira análise automática do scout; se a dúvida
  continuar, somente o exec leva a questão ao Breno.
- Não rode isso como substituto de `herdr-review` depois que o código já
  existe — nesse ponto a pergunta certa é "isso está correto", não "qual a
  melhor abordagem", e o formato de achado atômico do `herdr-review` serve
  melhor.
