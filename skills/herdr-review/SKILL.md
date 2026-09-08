---
name: herdr-review
description: "Dispara uma rodada de revisão cega e paralela pros dois revisores (<slug>-rev-1, <slug>-rev-2) de um space do Herdr, depois de terminar uma unidade de trabalho revisável. Use quando você mesmo (o exec) decidir que algo está pronto pra revisão, ou quando o usuário pedir explicitamente pra revisar/mandar pra revisão. Requer HERDR_ENV=1 e rodar dentro de um pane cujo agent se chama <slug>-exec, com <slug>-rev-1 e <slug>-rev-2 vivos no mesmo space."
---

# herdr-review

Sistematiza o que antes era feito na mão: pedir revisão cega e paralela pros dois
revisores de um projeto, classificar divergência, decidir o que corrigir sozinho e
o que escalar.

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

Depois de terminar uma unidade de trabalho — uma feature, um fix, um refactor
coeso — não a cada edit isolado. Você (o exec) decide sozinho quando pedir; não
espere o usuário mandar toda vez. Se tiver dúvida se o trabalho já está "pronto o
suficiente", prefira pedir revisão a adiar — o custo de uma rodada é bem menor
que o de um bug que passa direto.

## Descobrir seu próprio slug

Seu nome de agent no Herdr segue o padrão `<slug>-exec`. Resolva via `HERDR_PANE_ID`:

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

## Disparar a rodada

```bash
herdr-review-dispatch "$slug" --description "o que foi feito, por que, e o que é fora de escopo"
```

Isso congela o diff atual (`git diff` contra `HEAD`, incluindo untracked) em
`.herdr/review/<slug>-<n>/`, cria uma **pasta por revisor** dentro dela
(`<slug>-<n>/<slug>-rev-1/`, `<slug>-<n>/<slug>-rev-2/`), escreve o `request.md`
de cada um (protocolo genérico + `.herdr/reviewer.md` do projeto, se existir, +
sua descrição), dispara `<slug>-rev-1` e `<slug>-rev-2` em paralelo, cegos um do
outro, e espera os dois assentarem. Exige os dois `idle` **ou** `done` antes de
rodar — se algum estiver `working`/`blocked`, o script recusa em vez de
enfileirar.

Sem repo git (script solto, por exemplo)? Use `--files <path> [<path> ...]` em
vez de deixar o modo git tentar e falhar — revisa os arquivos listados
diretamente do disco, sem diff. `--base <ref>` muda a base do diff (default
`HEAD`). `--timeout <segundos>` muda o teto de espera por revisor (default
**1200 = 20 minutos**) — o script fica em silêncio dentro desse tempo se algum
revisor estiver `blocked` de verdade, então não estranhe demora sem output.

O script bloqueia até terminar e sai com **código 0 só se os dois** revisores
produziram veredito utilizável; qualquer `BLOCKED`/`TIMEOUT`/veredito
ausente-ou-vazio sai com 1 — não confie só no texto do stdout, cheque o exit
code se for encadear isso em algo automático.

## Ler e classificar

Leia os dois `verdict.md` que o comando apontou — cada um dentro da pasta do
seu próprio revisor (`<round_dir>/<slug>-rev-1/verdict.md`,
`<round_dir>/<slug>-rev-2/verdict.md`). Não use `agent read` nos panes dos
revisores pra isso — é scrape de tela, lossy; os arquivos são a fonte de
verdade.

Monte uma tabela por achado, em três classes:

| Classe | Quando | Quem trata |
|---|---|---|
| **CONFIRMADO** | Os dois revisores bateram no mesmo achado | Você corrige sozinho |
| **ÚNICO** | Só um achou — o outro simplesmente não mencionou | Você avalia e corrige se procede |
| **CONFLITO** | Um revisor **contesta explicitamente** a validade do que o outro apontou (não apenas deixou de encontrar) | Nunca você sozinho — veja abaixo |

## CONFLITO — rota aprovada, sem terceiro agent

Se houver uma discordância factual genuína, não crie pane ou agent novo e não
use AgentRelay nem o mecanismo nativo de subagentes da sua CLI. Redispare a
consulta pela rota aprovada de `herdr-ask`, usando os colegas fixos e o
contexto congelado exigido pelo protocolo. O resultado não substitui a
decisão humana nem autoriza o exec a descartar o achado.

Se um revisor estiver travado ou indisponível, redispare para o mesmo revisor
via `herdr-review`; não invente uma lente substituta. Se o caminho de
`herdr-ask` não puder ser executado, pare e reporte o bloqueio.

## Corrigir e ciclar

Aplique as correções dos achados CONFIRMADO (e ÚNICO que procedam) você mesmo. Pra
CONFLITO ou qualquer achado que você considere descartar: **pare e pergunte ao
usuário** — nunca decida sozinho que um achado não procede.

Se corrigiu algo, pode rodar `herdr-review-dispatch` de novo pra uma nova rodada
(cria `<slug>-2`, etc. automaticamente). **Máximo 2 rodadas de correção** — na
terceira, pare e leve o estado pro usuário decidir, mesmo que ainda haja achado
aberto.

Pare imediatamente pro usuário, sem esperar o teto de rodadas, se aparecer:
mudança arquitetural nas correções, achado de severidade alta e controverso, ou
recomendações mutuamente incompatíveis entre os dois revisores.

## O que não fazer

- Não rode `herdr-review-dispatch` em cima de trabalho não commitado que não é
  seu — confirme que o diff é da unidade que você mesmo terminou.
- Não trate `TIMEOUT` ou revisor `BLOCKED` como aprovação.
- Não decida sozinho que um CONFLITO não procede, mesmo que a rota de
  `herdr-ask` produza uma posição favorável.
