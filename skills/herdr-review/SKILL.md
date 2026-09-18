---
name: herdr-review
description: "Dispara uma rodada de revisão cega e paralela pros dois revisores (<slug>-rev-1, <slug>-rev-2) de um space do Herdr, depois de terminar uma unidade de trabalho revisável, com terceira análise automática pelo <slug>-scout quando o exec julgar necessário após duas rodadas. Use quando você mesmo (o exec) decidir que algo está pronto pra revisão, ou quando o usuário pedir explicitamente pra revisar/mandar pra revisão. Requer HERDR_ENV=1 e rodar dentro de um pane cujo agent se chama <slug>-exec."
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

O lifecycle do Herdr não acompanha a escrita do turno: `agent prompt --wait`
pode devolver `done` antes de o revisor publicar `verdict.md`. O dispatcher
trata `idle`/`done` como candidato e só assenta depois de observar um
`verdict.md` regular, não vazio e novo (ou alterado) desde o disparo. Se o
prazo terminar sem o artefato, registra `artifact_missing` e falha; não há
aprovação com `verdict_bytes: 0`. O `metrics.json` é publicado por um
temporário no mesmo diretório e `os.replace`, evitando que uma leitura
concorrente veja JSON pela metade.

Os panes de `rev-1`, `rev-2` e `scout` são headless. Texto residual na caixa de
composição, inclusive uma letra digitada por engano, é descartável e não para
o dispatcher; somente um diálogo real do CLI, recusado pelo canal oficial como
`agent_blocked`, bloqueia o despacho. O dispatcher não faz reset automático nem
exige que modelo ou reasoning permaneçam iguais; `herdr-swap` é o caminho
explícito para recriar o pane com handoff. A guarda contra composição humana
permanece no `-exec` e no `herdr-swap`.

Antes de criar os prompts, o dispatcher revalida sob lock curto o snapshot
capturado de cada alvo: status, pane, workspace, cwd, sessão, revision e
`state_change_seq`, além da ausência de diálogo. Qualquer mudança aborta sem
enviar e fica registrada em `preflight_error`. Se `agent_prompt --wait` retornar
`agent_prompt_stalled`, a mesma checagem ocorre antes do único retry; um alvo já
`working`, ou cuja sequência avançou, recebe `agent wait` e não um segundo
prompt. O reenvio só ocorre com a identidade e o estado idle/done inalterados.

Se `rev-1` ou `rev-2` não estiver registrado, o dispatcher tenta a recuperação
idempotente por `herdr-bootstrap --slug <slug>` antes de criar a rodada, espera
o papel novo ficar pronto e revalida seu projeto. A tabela define modelo/kind/
raciocínio apenas para uma inicialização sem pane-base; o dispatcher não limpa
um pane já em uso para aplicar defaults. Uma recriação deliberada usa
`herdr-swap`, copia o handoff e registra o perfil efetivo como evidência. Se a
recuperação realmente falhar, o erro é controlado e a rodada não é parcialmente
enviada.

## Ler e classificar

Leia os dois `verdict.md` que o comando apontou — cada um dentro da pasta do
seu próprio revisor (`<round_dir>/<slug>-rev-1/verdict.md`,
`<round_dir>/<slug>-rev-2/verdict.md`). Não use `agent read` nos panes dos
revisores pra isso — é scrape de tela, lossy; os arquivos são a fonte de
verdade.

Cada achado deve trazer não só o problema, mas também **solução proposta**,
**validação** e eventual **decisão necessária**. “Não determinada”, com a
lacuna e a pergunta explícitas, é válido quando não houver base segura; um
achado sem nenhuma dessas formas de encaminhamento é um veredito incompleto.
`APPROVE` deve declarar **ação necessária: nenhuma**.

Monte uma tabela por achado, em três classes:

| Classe | Quando | Quem trata |
|---|---|---|
| **CONFIRMADO** | Os dois revisores bateram no mesmo achado | Você corrige sozinho |
| **ÚNICO** | Só um achou — o outro simplesmente não mencionou | Você avalia e corrige se procede |
| **CONFLITO** | Um revisor **contesta explicitamente** a validade do que o outro apontou (não apenas deixou de encontrar) | Nunca você sozinho — veja abaixo |

## ESCALONAMENTO — terceira análise automática pelo scout

`rev-1` e `rev-2` têm no máximo duas rodadas de correção. Se, depois delas,
você julgar necessária uma análise adicional para uma disputa, indecisão ou
achado aberto, chame automaticamente o scout. Não crie pane ou agent novo e
não use AgentRelay nem o mecanismo nativo de subagentes da sua CLI. A chamada
ao scout não exige autorização prévia do Breno. Escreva uma pergunta focada e,
se necessário, congele os arquivos relevantes para o scout:

```bash
herdr-ask "$slug" --question-file /caminho/da-duvida.md \
  --reviewer scout --context /caminho/material-relevante
```

`herdr-ask --reviewer scout` entrega a terceira análise automática diretamente
ao pane existente; não há reset implícito nem sonda que bloqueie por troca de
perfil. Use `herdr-swap` antes, quando uma recriação limpa for necessária, e
`herdr-context-watch` para medir o contexto sem enviar comandos. Model e
reasoning capturados antes/depois são evidência; se ficarem desconhecidos ou
diferentes, registre isso sem inventar default e sem abortar a consulta.

O scout analisa a dúvida e devolve `answer.md` exclusivamente ao `<slug>-exec`.
Ele não fala diretamente com o usuário, não decide pelo space e não faz
commit. Se a evidência continuar insuficiente ou houver divergência, deve
registrar explicitamente a incerteza para o exec. Depois de ler a resposta, se
você ainda julgar necessária outra análise, ou se o scout tiver devolvido
INCERTEZA/DIVERGÊNCIA, pare e consulte o Breno. Se o caminho de `herdr-ask`
não puder ser executado, pare e reporte o bloqueio.

## Corrigir e ciclar

Antes de corrigir ou responder ao Breno, o `<slug>-exec` transforma os dois
vereditos em uma síntese explícita. Para cada achado, mostre:

| Campo | Conteúdo |
|---|---|
| **Achado e impacto** | O problema confirmado, único ou em conflito e por que importa |
| **Solução proposta** | A recomendação do revisor, ou a solução formulada pelo exec quando o revisor não pôde determiná-la |
| **Decisão do exec** | Corrigir, descartar, escalar ao scout ou consultar o Breno |
| **Próximo passo e validação** | A ação imediata e como saberemos que terminou |

Não entregue somente a lista de P0–P3. O revisor propõe; o exec decide,
coordena a correção e comunica a síntese ao Breno. Se a recomendação do
revisor for insuficiente, o exec deve completar a proposta ou declarar a
incerteza — nunca deixar o usuário descobrir a solução perguntando de novo.

Aplique as correções dos achados CONFIRMADO (e ÚNICO que procedam) você mesmo.
Para CONFLITO ou qualquer achado que você considere descartar, use a terceira
análise automática do scout antes de consultar o Breno — nunca decida sozinho
que um achado não procede.

Se corrigiu algo, pode rodar `herdr-review-dispatch` de novo pra uma nova rodada
(cria `<slug>-2`, etc. automaticamente). **Máximo 2 rodadas de correção** — na
terceira, se necessária, use uma única análise automática do scout em vez de
redisparar `rev-1`/`rev-2`. Depois do scout, qualquer nova necessidade de
análise ou divergência/incerteza volta ao Breno.

Antes do scout, mudança arquitetural nas correções, achado de severidade alta e
controverso ou recomendações mutuamente incompatíveis são motivos para o exec
julgar necessária a terceira análise automática; não pedem autorização prévia.
Depois do scout, se uma dessas condições continuar, ou se ele devolver
INCERTEZA/DIVERGÊNCIA, somente o exec consulta o Breno.

## O que não fazer

- Não rode `herdr-review-dispatch` em cima de trabalho não commitado que não é
  seu — confirme que o diff é da unidade que você mesmo terminou.
- Não trate `TIMEOUT` ou revisor `BLOCKED` como aprovação.
- Não decida sozinho que um CONFLITO não procede, mesmo que a rota de
  `herdr-ask` produza uma posição favorável.
