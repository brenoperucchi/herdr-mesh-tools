# herdr-mesh-tools

Scripts e skills pessoais construídos em cima do [Herdr](https://github.com) (o CLI/
multiplexador de terminal para coding agents) — não é o Herdr em si, é a camada de
automação por cima dele: bootstrap de spaces/panes/agents, notificação de background,
troca de kind do executor preservando contexto, e o ciclo de revisão cega/paralela
entre dois revisores independentes.

Os arquivos aqui são a fonte real; `~/.local/bin/` e `~/.agents/skills/` apontam pra
cá via symlink (o mesmo padrão usado no dotfiles). Editar em qualquer um dos dois
lugares edita o mesmo arquivo.

## `bin/`

- **`herdr-bootstrap`** — garante spaces/panes/agents do ambiente de trabalho
  (idempotente). Define, por space, quem é o executor e quem são os dois revisores
  (kind, modelo, effort), e arma um `herdr-notify-watch` pra cada `*-exec`.
- **`herdr-notify-watch`** — daemon (`flock`-protegido) que notifica via
  `omarchy-notification-send` quando um agent `*-exec` muda de estado de verdade
  (edge-triggered em `state_change_seq`, não em polling ingênuo de status).
- **`herdr-review-dispatch`** — dispara uma rodada de revisão cega e paralela pros
  dois revisores de um space (`--verify` faz verificação com um único revisor,
  mais barata). Congela o diff (incluindo untracked), isola cada revisor em
  subdiretório próprio, grava `metrics.json` por rodada pra correlação de custo.
- **`herdr-swap-exec`** — troca o kind (`claude`/`codex`) do agent `<slug>-exec` de
  um space, preservando contexto via handoff escrito em arquivo pelo agent que sai.

## `skills/`

Skills do Claude Code / Codex (compartilhadas via symlink em `~/.claude/skills/` e
`~/.codex/skills/`):

- **`herdr`** — uso geral do CLI Herdr (inspecionar/controlar panes, tabs, workspaces,
  agents).
- **`herdr-review`** — protocolo de disparo + classificação de achados
  (CONFIRMADO/ÚNICO/CONFLITO) do ciclo de revisão, com teto de 2 rodadas.
- **`herdr-swap`** — protocolo de troca de executor com handoff de contexto.

## Contexto de projeto

Cada projeto real que usa esse ciclo mantém seu próprio `.herdr/reviewer.md`
(invariantes específicas do domínio pros revisores) — isso vive no repo de cada
projeto, não aqui.
