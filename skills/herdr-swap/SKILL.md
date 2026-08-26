---
name: herdr-swap
description: "Troca o kind (claude/codex/etc) do agent <slug>-exec de um space do Herdr, preservando contexto via handoff em arquivo escrito pelo agent que está saindo. Use quando o usuário pedir pra trocar/mudar o executor de um projeto pra outro modelo — geralmente por limite de uso/token, ou pra testar outro modelo no papel de exec. Requer HERDR_ENV=1."
---

# herdr-swap

Troca quem ocupa o papel de `<slug>-exec` sem perder o fio do que estava sendo
feito — o handoff é escrito pelo próprio agent que sai, não é scrape de buffer
(isso é lossy e não atravessa entre CLIs diferentes).

```bash
test "${HERDR_ENV:-}" = 1
```

## Quando usar

Só quando o usuário pedir explicitamente — troca de exec não é algo pra você
decidir sozinho como o `herdr-review` (isso muda quem está no comando do
projeto, não é uma rotina de qualidade). Motivo típico: bateu limite semanal de
um provedor e o usuário quer mover a execução pra outro; ou quer comparar como
um modelo diferente se sai no papel de exec.

## Uso

```bash
herdr-swap-exec <slug> <kind> [-- <agent-args...>]
```

Exemplos:

```bash
herdr-swap-exec omabackup codex
herdr-swap-exec mfc claude -- --model opus
```

`<slug>-exec` precisa existir e estar `idle` ou `done` — o script recusa trocar
em cima de `working`/`blocked`. Ele:

1. Pede pro exec atual escrever um resumo de handoff em
   `.herdr/handoff/<slug>-<timestamp>-<pid>.md` (o que estava fazendo, decisões,
   o que falta, arquivos relevantes) e confirma que o arquivo foi escrito antes
   de continuar — se não escrever, aborta e não mexe no processo antigo.
2. Divide um pane novo do lado, fecha o antigo (é assim que o processo anterior
   é encerrado — pedir pra CLI sair educadamente via `ctrl+d`/`ctrl+c` se
   mostrou pouco confiável, não confie nisso). **A partir daqui o exec antigo já
   morreu** — se algo falhar depois disso (passo 3 ou 4), não há mais como
   voltar atrás, só seguir em frente.
3. Sobe o novo `kind` no pane novo, com o mesmo nome `<slug>-exec`.
4. Manda o novo agent ler o handoff antes de fazer qualquer coisa — sem esperar
   resposta.

**Se o passo 3 falhar** (o script imprime "o pane X está aguardando" com o
caminho do handoff): o exec antigo já morreu e o novo nunca subiu — o pane fica
com um shell nu. Não é dado perdido, o handoff está salvo em disco. Suba na mão:
`herdr agent start <slug>-exec --kind <kind> --pane <pane-que-o-script-citou>`,
depois `herdr agent prompt <slug>-exec "Leia <caminho-do-handoff> antes de
qualquer coisa"`.

Diálogos de startup (ex: confiar num diretório, comum na primeira vez que um
`kind` roda ali) são reconhecidos por padrão de texto e aprovados
automaticamente **só se o padrão bater** com algo conhecido ("Do you trust" /
"confia"). Se o script parar reportando um diálogo que não reconhece, olhe o
pane na mão — ele não aprova diálogo desconhecido às cegas.

## Depois do swap

O novo exec só recebeu a instrução de ler o handoff — não force mais nada além
disso. Deixe ele ler e retomar no próprio ritmo; se o resumo não for suficiente,
é normal ele perguntar de volta.

## O que não fazer

- Não rode em cima de um exec `working`/`blocked` — o script já recusa, não
  tente contornar isso.
- Não assuma que o handoff cobre tudo perfeitamente — é um resumo escrito por
  um agent sobre si mesmo, não um dump completo. Se o novo exec parecer perdido,
  isso é sinal de que o handoff ficou raso, não de que o mecanismo falhou.
