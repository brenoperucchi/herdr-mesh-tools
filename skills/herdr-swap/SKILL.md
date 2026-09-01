---
name: herdr-swap
description: "Troca o kind (claude/codex/grok/etc) do agent <slug>-<role> (exec, rev, rev-2, scout, ...) de um space do Herdr, preservando contexto via handoff em arquivo escrito pelo agent que está saindo. Use quando o usuário pedir pra trocar/mudar quem ocupa um papel num projeto pra outro modelo — geralmente por limite de uso/token, ou pra testar outro modelo nesse papel. Requer HERDR_ENV=1."
---

# herdr-swap

Troca quem ocupa um papel (`<slug>-<role>` — exec, rev, rev-2, scout, ...) sem
perder o fio do que estava sendo feito — o handoff é escrito pelo próprio
agent que sai, não é scrape de buffer (isso é lossy e não atravessa entre
CLIs diferentes). A mecânica é a mesma pra qualquer papel; só muda o nome.

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

Só quando o usuário pedir explicitamente — trocar quem ocupa um papel não é
algo pra você decidir sozinho como o `herdr-review` (isso muda quem está no
comando do projeto ou revisando, não é uma rotina de qualidade). Motivo
típico: bateu limite semanal de um provedor e o usuário quer mover pra outro;
ou quer comparar como um modelo diferente se sai nesse papel.

## Uso

```bash
herdr-swap <slug> <role> <kind> [-- <agent-args...>]
```

Exemplos:

```bash
herdr-swap omabackup exec codex
herdr-swap mfc rev-2 grok
herdr-swap mfc exec claude -- --model opus
```

`<slug>-<role>` precisa existir e estar `idle` ou `done` — o script recusa
trocar em cima de `working`/`blocked`. Ele:

0. Antes de mandar qualquer coisa pro pane antigo (inclusive o pedido de
   handoff), lê o pane (`--source detection`) e aborta se parecer ter
   composição humana não enviada ou diálogo pendente. **Isso é heurística, não
   garantia** — existe corrida real entre essa leitura e a ação seguinte
   (alguém pode digitar bem depois do read). Reduz a janela, não a fecha.
1. Pede pro agent atual escrever um resumo de handoff em
   `.herdr/handoff/<slug>-<timestamp>-<pid>.md` (o que estava fazendo, decisões,
   o que falta, arquivos relevantes) e confirma que o arquivo foi escrito antes
   de continuar — se não escrever, aborta e não mexe no processo antigo.
2. Divide um pane novo do lado e sobe o novo `kind` ali sob um nome
   **provisório** (`<slug>-<role>-new`) — o agent antigo **continua vivo e
   intocado** nesse momento.
3. Só depois de confirmar que o provisório respondeu (subiu de verdade, não
   travou num diálogo desconhecido), fecha o pane antigo — **esse é o ponto de
   não-retorno real**, tudo antes dele é reversível — e renomeia o provisório
   pro nome final (`<slug>-<role>-new` → `<slug>-<role>`) via `herdr agent
   rename`.
4. Manda o novo agent (já com o nome final) ler o handoff antes de fazer
   qualquer coisa — sem esperar resposta.

**Se o passo 2 falhar** (o novo kind não sobe): o script já fecha sozinho o
pane provisório e imprime que o agent antigo **não foi tocado** — nada a
recuperar, o handoff fica salvo em disco pra tentar de novo. **Se o passo 3
falhar depois do close do antigo** (rename não vai): o script avisa que o novo
agent está vivo, só que ainda com o nome provisório — renomeie na mão:
`herdr agent rename <pane-que-o-script-citou> <slug>-<role>`.

Diálogos de startup (ex: confiar num diretório, comum na primeira vez que um
`kind` roda ali) são reconhecidos por padrão de texto e aprovados
automaticamente **só se o padrão bater** com algo conhecido ("Do you trust" /
"confia"). Se o script parar reportando um diálogo que não reconhece, olhe o
pane na mão — ele não aprova diálogo desconhecido às cegas.

## Depois do swap

O novo agent só recebeu a instrução de ler o handoff — não force mais nada além
disso. Deixe ele ler e retomar no próprio ritmo; se o resumo não for suficiente,
é normal ele perguntar de volta.

## O que não fazer

- Não rode em cima de um agent `working`/`blocked` — o script já recusa, não
  tente contornar isso.
- Não assuma que o handoff cobre tudo perfeitamente — é um resumo escrito por
  um agent sobre si mesmo, não um dump completo. Se o novo agent parecer perdido,
  isso é sinal de que o handoff ficou raso, não de que o mecanismo falhou.
- **Nunca encerre um agent mandando `/exit` ou `ctrl+d`/`ctrl+c` como texto pra
  um pane que pode estar em uso** (nem na mão, nem em script novo) — se houver
  algo digitado e não confirmado na caixa de composição, o texto de saída se
  junta a ele e é ENVIADO como mensagem, não descartado. Pra encerrar um
  processo, use `herdr pane close <pane_id>` — ele mata o processo direto, sem
  passar pela caixa de entrada, então na pior hipótese *descarta* o rascunho
  não confirmado em vez de *submetê-lo*. Dois incidentes reais confirmaram isso
  (ver `~/.herdr/ask/herdr-6/` pra contexto completo).
- Pra ajustar só um parâmetro (ex: effort/model) de um agent que já está no
  `kind` certo, não rode um swap completo — prefira `herdr pane close` +
  `herdr pane split` + `<cli> resume <session-id> -c ...` no mesmo `kind`, sem
  trocar de CLI. Evita a janela de risco inteira do handoff cross-CLI.

## Próximos passos identificados (não implementados ainda)

- `--dry-run`: mostrar o plano (handoff que seria pedido, panes que seriam
  tocados) sem executar `close`/`start` de verdade.
- Log de auditoria em append (`.herdr/handoff/swap-log.md`: quando, quem,
  de/para, status de cada etapa) — hoje um incidente só fica registrado se
  alguém lembrar de reconstruir de memória.
- Lock por slug pra evitar duas trocas simultâneas no mesmo `<slug>-exec`.
