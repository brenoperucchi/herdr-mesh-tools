---
name: herdr-ask
description: "Dispara uma consulta de design cega e paralela pros dois consultores (<slug>-rev-1, <slug>-rev-2) de um space do Herdr, pra uma pergunta de design ABERTA — não uma revisão de código. Use quando precisar de uma segunda (e terceira) opinião independente antes de decidir uma arquitetura, um trade-off, ou qualquer questão sem resposta óbvia — antes de construir, não depois. Requer HERDR_ENV=1 e rodar dentro de um pane cujo agent se chama <slug>-exec, com <slug>-rev-1 e <slug>-rev-2 vivos no mesmo space."
---

# herdr-ask

Mesma mecânica cega/paralela do `herdr-review` (isolamento por diretório,
congelamento de contexto, os dois assentando antes de você ler qualquer
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
os dois assentarem.

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

## Ler e reconciliar

Leia os `answer.md` de cada consultor (`<round_dir>/<slug>-rev-1/answer.md`,
`<round_dir>/<slug>-rev-2/answer.md`) — cada resposta tem: a decisão numa
frase, as premissas explícitas que a sustentam, e o argumento.

**Não trate isso como revisão de código.** A reconciliação certa depende das
premissas, não só da conclusão:

| Conclusões | Premissas | O que é | O que fazer |
|---|---|---|---|
| iguais | iguais | convergência real | siga com confiança |
| opostas | a mesma premissa carregando o peso | discordância genuína | leve pro usuário decidir |
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
  oposta) — isso é exatamente o caso que existe pra levar ao usuário.
- Não rode isso como substituto de `herdr-review` depois que o código já
  existe — nesse ponto a pergunta certa é "isso está correto", não "qual a
  melhor abordagem", e o formato de achado atômico do `herdr-review` serve
  melhor.
