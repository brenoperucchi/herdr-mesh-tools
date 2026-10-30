---
name: herdr-review
description: "Dispara revisão por unidade em nível 1 (um revisor, com justificativa) ou nível 2 (dois revisores cegos, padrão) (<slug>-rev-1, <slug>-rev-2) de um space do Herdr, depois de terminar uma unidade de trabalho revisável, com terceira análise automática pelo <slug>-scout quando o exec julgar necessário após seis rodadas. Use quando você mesmo (o exec) decidir que algo está pronto pra revisão, ou quando o usuário pedir explicitamente pra revisar/mandar pra revisão. Requer HERDR_ENV=1 e rodar dentro de um pane cujo agent se chama <slug>-exec."
---

# herdr-review

O exec escolhe o nível por unidade. Nível 1 usa um revisor e exige justificativa.
Nível 2 é o padrão, usa dois revisores cegos e é obrigatório nos gatilhos abaixo.
O exec lê os pareceres, classifica divergências, decide a correção e registra o
encaminhamento. Resultado operacional completo não aprova a mudança por si só.

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

## Escolher o nível e disparar sem bloquear o exec

```bash
herdr-review-dispatch "$slug" --nivel 2 --description "unidade e escopo"
herdr-review-dispatch "$slug" --nivel 1 --justificativa "correção textual local" --description "unidade e escopo"
```

`--nivel` aceita 1 ou 2; omiti-lo mantém 2. Nível 1 exige `--justificativa` não
vazia, registrada em cada `request.md` e no `metrics.json`. Escolhe o primeiro
revisor idle/done entre rev-1 e rev-2. `--revisor rev-1|rev-2` escolhe explicitamente.
Se não houver revisor pronto, recupera somente um papel ausente; nunca interrompe
um working/blocked. Não sobe o segundo revisor no nível 1.

Use nível 2 se houver chave/segredo/.env, dado privado (Guardian, MFC, cerebro),
migração de dados/pasta/serviço, produção (systemd, gateway, deploy), ou
protocolo/schema/contrato entre agents. `--gatilho <nome>` declara essa condição
e pode ser repetido. Qualquer gatilho recusa nível 1. Na dúvida, use 2; a
detecção por caminho não substitui a declaração do exec sobre o conteúdo.

Os padrões globais estão em `bin/_herdr_review_policy.py`. O projeto pode somar
padrões em `.herdr/review-gatilhos.json`, sem remover os globais:

```json
{"version": 1, "gatilhos": {"pagamentos": ["billing/*", "src/checkout.py"]}}
```

Os globs comparam caminho relativo, basename e caminho absoluto canônico, sem
distinguir maiúsculas. O caminho absoluto participa em todas as categorias:
um projeto gateway ou privado permanece nível 2 mesmo numa edição textual.
Configuração inválida ou inventário incerto recusa nível 1; nível 2 registra a
incerteza. O inventário inclui índice, worktree, untracked e origem de rename.

O comando congela o diff contra HEAD (ou `--base <ref>`) em
`.herdr/review/<slug>-<n>/`, com uma pasta por revisor selecionado, request e
verdict separados. `--files <path> [<path> ...]` congela arquivos sem exigir diff.
No modo diff, os revisores leem também index.patch (base para índice staged) e
worktree.patch (índice para diretório de trabalho); as camadas independentes
preservam alterações que se cancelam no diff líquido.
Revisores de nível 2 continuam cegos: nunca leem a pasta ou o parecer do colega.

O dispatcher espera artefatos. Execute-o em processo separado com stdin fechado,
log e arquivo de exit code, e devolva o controle ao usuário. Não use sleep,
polling ou espera de revisão em primeiro plano. `--timeout <segundos>` mantém o
teto por revisor (padrão 1200); expirar não interrompe o agent nem seu shell.

## Resultado completo, parcial ou falha

- Exit 0, `review_status: "completa"`: todos os revisores exigidos produziram
  parecer utilizável. O exec ainda precisa avaliar os achados.
- Exit 3, `review_status: "parcial"`: houve parecer utilizável, mas faltou outro.
  Métricas registram quem faltou e o motivo. Parcial NUNCA é aprovação.
- Exit 1, `review_status: "falha"`: nenhum parecer utilizável ou material
  congelado alterado durante a rodada; não há cobertura válida concluída. Erro de argumentos/política pode sair com 2.

No nível 2, BLOCKED, TIMEOUT, artifact_missing, ausência no registro ou recusa do
can-send de um revisor não descartam o parecer do outro. Antes de encerrar uma
unidade com gatilho como revisada, repita o revisor faltante ou consulte Breno.
Use `--complementar <round_dir>` (com `--revisor rev-1|rev-2` opcional) para
repetir só o faltante de nível 2 parcial. O complemento usa o mesmo material e
requests congelados, confere hashes e não entrega o parecer do colega. Não
altera o número da rodada, não reduz o nível e registra a tentativa nas métricas.
Mudança de material, parecer anterior ou resultado ambíguo interrompido exige
diagnóstico; não há reenvio automático. Rodadas antigas sem hashes não podem
ser complementadas: refaça o nível 2 ou consulte Breno.
Uma nova rodada nível 1 não completa automaticamente uma rodada nível 2 parcial.
`--verify` continua uma checagem fechada de achados anteriores; suas métricas
marcam `coverage_scope: "verification_only"`, sem convertê-la em revisão nova
de nível 2 ou aprovação de uma unidade parcial.

`idle`/`done` só é candidato: precisa de `verdict.md` regular, não vazio e novo
ou alterado desde o disparo. Sem artefato no prazo, `artifact_missing`. As
métricas são publicadas atomicamente. `approval: false` distingue cobertura da
decisão do exec sobre o conteúdo dos pareceres.

Antes de cada entrega, inclusive a headless, herdr-can-send precisa retornar 0;
códigos 1 a 4 recusam, sem limpar composição. Cada alvo conserva sua validação
sob lock e snapshot de identidade/status/sequência. Um alvo recusado não recebe
prompt; os outros podem seguir. Retry só com identidade e estado idle/done
inalterados. Alvo working ou sequência avançada recebe espera do dispatcher,
sem segundo prompt. Não há reset implícito; recriação deliberada usa herdr-swap.

## Ler e classificar

Leia os `verdict.md` disponíveis dos revisores selecionados que o comando apontou — cada um dentro da pasta do
seu próprio revisor (`<round_dir>/<slug>-rev-1/verdict.md`,
`<round_dir>/<slug>-rev-2/verdict.md`). Não use `agent read` nos panes dos
revisores pra isso — é scrape de tela, lossy; os arquivos são a fonte de
verdade.

O contrato canônico dos campos de encaminhamento fica em
`bin/_herdr_dispatch.py:SOLUTION_CONTRACT_REVIEWER`; o dispatcher o insere no
request de cada rodada. Use esse contrato literalmente para que um achado não
seja entregue sem solução/validação ou uma declaração explícita de lacuna.

No nível 1, os achados têm uma única lente. No nível 2, monte uma tabela por achado, em três classes:

| Classe | Quando | Quem trata |
|---|---|---|
| **CONFIRMADO** | Os dois revisores bateram no mesmo achado | Você corrige sozinho |
| **ÚNICO** | Só um achou — o outro simplesmente não mencionou | Você avalia e corrige se procede |
| **CONFLITO** | Um revisor **contesta explicitamente** a validade do que o outro apontou (não apenas deixou de encontrar) | Nunca você sozinho — veja abaixo |

## ESCALONAMENTO — terceira análise automática pelo scout

`rev-1` e `rev-2` têm no máximo seis rodadas de correção. Se, depois delas,
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
`herdr-context-watch` para medir o contexto passivamente sem enviar comandos;
uma sonda `/status` só ocorre com `--probe` explícito e qualquer falha tenta
cancelar o diálogo com `Esc`. Model e
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

Antes de corrigir ou responder ao Breno, o `<slug>-exec` usa o contrato
canônico `SOLUTION_CONTRACT_EXEC` de `bin/_herdr_dispatch.py` para transformar
os vereditos em uma síntese explícita. O revisor propõe; o exec decide,
coordena a correção e comunica a síntese ao Breno. Se a recomendação do
revisor for insuficiente, o exec deve completar a proposta ou declarar a
incerteza — nunca deixar o usuário descobrir a solução perguntando de novo.

Aplique as correções dos achados CONFIRMADO (e ÚNICO que procedam) você mesmo.
Para CONFLITO ou qualquer achado que você considere descartar, use a terceira
análise automática do scout antes de consultar o Breno — nunca decida sozinho
que um achado não procede.

Se corrigiu algo, pode rodar `herdr-review-dispatch` de novo pra uma nova rodada
(cria `<slug>-2`, etc. automaticamente). **Máximo 6 rodadas de correção** — depois
delas, se necessária, use uma única análise automática do scout em vez de
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


## Grupo na tab do coordenador

Grupos registrados usam um `coordinator_name` explícito; o nome não precisa
terminar em `-exec`. Use o slug configurado, sem inferi-lo do label da tab.
O primeiro grupo é `guardian2`, coordenado por `guardian2-exec`, com raiz real
`/home/brenoperucchi/Devs/guardian2`. Rode a partir dessa raiz e use o slug
`guardian2` na chamada normal. Revisores ausentes sobem somente na tab atual
do coordenador vivo; a raiz de `cwd` e `foreground_cwd` deve coincidir.
Para fechar o par após o resultado: `herdr-release guardian2 --only reviewers`.
O grupo recusa liberação de agent ocupado, vínculo divergente ou rodada
incompleta mesmo com `--force`. Bia não tem grupo registrado nesta entrega.
