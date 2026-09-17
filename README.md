# herdr-mesh-tools

Personal scripts and skills built on top of [Herdr](https://herdr.dev) (the
terminal multiplexer for coding agents) — this is not Herdr itself, it's the
automation layer on top of it: workspace/pane/agent bootstrap, background
notifications, swapping the executor's kind while preserving context, and a
blind/parallel dispatch mechanism used for both code review and open design
questions between two independent reviewers.

The files here are the source of truth; `~/.local/bin/` and `~/.agents/skills/`
point back to this repo via symlink (the same pattern used for dotfiles).
Editing either location edits the same file. `~/.agents/skills/` is what makes
Claude Code and Codex share one copy of each skill.

## The shape of a space

Every project gets one Herdr workspace with the same crew, named after the
project's **slug**:

| agent | tab | role |
|---|---|---|
| `<slug>-exec` | 1 | does the work; the interactive session you talk to |
| `<slug>-rev-1` | 1 | first reviewer (model `gpt-5.6-luna` or `gpt-6-astra`) |
| `<slug>-rev-2` | 1 | second reviewer (model `opus-5`) — deliberately a different lens |
| `<slug>-scout` | 2 | parallel reading/exploration (model `gpt-6-astra`, `fable-5` or `opus-5`); its own tab because the work is a different kind |

The `llm-scout` workspace is a recorded `Need you` exception: it remains on
`gpt-5.6-sol` with `xhigh` reasoning until the owner decides otherwise; no
automatic migration changes it.

The two reviewers are the point: they review **blind to each other**, and a
finding both reach independently means something different from a finding only
one reaches. `herdr-review-dispatch` and `herdr-ask` exist to keep that
blindness honest.

The slug is usually the workspace label lowercased, but not always — the
workspace `llm-gateway` has slug `llm`, because that is how its agents were
named before the table existed. `SPACES` in `herdr-bootstrap` carries the
exception, and the comment next to it explains why.

## `bin/`

### Setting up

- **`herdr-add-space <label> <cwd>`** — registers a new space in
  `herdr-bootstrap`'s `SPACES` table and lets the bootstrap create it. Does not
  create workspaces/panes/agents itself: a parallel path would produce spaces
  the table does not know about, with no reviewer effort and no notify-watch,
  that nothing would recreate later. `--note` matters — the table earns its
  keep through the comments explaining *why* each space exists, so an entry
  without one lands with a visible TODO.
- **`herdr-bootstrap`** — ensures the workspaces/panes/agents of the working
  environment exist (idempotent; only ever acts on an empty pane). Defines, per
  workspace, who the executor is and who the two reviewers are (kind and
  model), and arms a `herdr-notify-watch` for each `*-exec`. Run it from
  anywhere: it reads absolute paths from its own table, not the cwd.
  `--dry-run` first — it has already caught an entry gone stale after a
  workspace was renamed by hand, which would have created a duplicate. A
  dispatcher can target one row with `--slug <slug>` (or `--space <label>`) to
  restore a missing mandatory agent without touching other spaces.
- **`herdr-agents [--name <cwd-filter>]`** — one tabular line per agent: status,
  cwd, kind, **model**, **effort**, **src**, name, pane, tab. The `src` column
  says where the model/effort actually came from, because the three sources
  disagree in practice:
  - `argv` — pinned at launch. The only one an operator chose.
  - `thread` — the process came up as `codex resume <uuid>`; the values are the
    session's final state, read from its rollout, and are authoritative.
    Measured 2026-09-10:
    resume restores this faithfully — `llm-bench-scout` runs `gpt-5.6-luna max`
    while the global default is `gpt-6-astra`, which would be impossible if it
    fell back to the default. Read the *last* record, not the first: a Codex
    `thread_settings_applied` event was seen demoting `high` to `medium`
    mid-session.
  - `thread?` — a Claude agent under `--resume` that has **not answered since
    the last `/model`**, so the log cannot be trusted. The model prints as
    `(unknown)`:
    the session file is a *log* that a live `/model` does not update until the
    next message, and `settings.json` is global. `herdr pane read <pane>`
    settles it. The guessed name is deliberately **not** printed anywhere,
    including `SRC`: on 2026-09-10 this column read `claude-fable-5-1` for four
    agents already switched by hand to Sonnet 5 and Opus 5, and
    `herdr-agents | grep -i fable` sent the user chasing a ghost twice. In a
    field people use to hunt cost, a stale guess is worse than an admitted
    unknown — and keeping it in any column just moves the false positive.

    A Claude agent that *has* answered since that change reads as plain
    `thread` and does print its model. The test is the timestamp of the last
    log entry carrying a `model` field, compared against the mtime of
    `settings.json` (which `/model` rewrites). File mtime alone does not work:
    measured, `acervo-exec`'s log was written three minutes *after* the switch
    and still held the old model, because that write was a `system` entry with
    no model in it. `model` only appears on assistant replies.
  - `global` — the `config.toml`/`settings.json` default, which the interactive
    `/model` rewrites, silently changing every agent that inherits it.

### Running the cycle

- **`herdr-review-dispatch`** — fires a blind, parallel code-review round to
  the two reviewers of a workspace (`--verify` runs a cheaper single-reviewer
  verification pass). Freezes the diff (including untracked files), isolates
  each reviewer in its own subdirectory, and writes a per-round `metrics.json`
  for cost correlation. The dispatcher leaves reviewer panes intact and does
  not enforce model/reasoning preservation; those guards caused valid rounds
  to abort when the CLI reported a different effective profile. Recreate a
  pane deliberately with `herdr-swap`, which transfers a handoff before
  closing the old process. Use `herdr-context-watch` for periodic,
  read-only context measurements.
- **`herdr-ask`** — same blind/parallel dispatch mechanism, for an open
  design question instead of a code diff (`--question-file`, optionally
  `--context <path>...` to freeze reference material, `--reviewer
  rev|rev-1|rev-2|scout` for a single consultant — `rev` and `rev-1` are
  aliases of the same role, while `scout` is the automatic third analysis when
  the exec needs it after two correction rounds). The response format
  is a decision with explicit premises, and the dispatcher leaves every
  reviewer/scout context intact. Recreate a pane deliberately with
  `herdr-swap`; `herdr-context-watch` reports context pressure without
  sending commands. Responses go to `.herdr/ask/`, a namespace separate from
  `.herdr/review/`. A scout response returns to the `-exec`; it never speaks
  directly to the user or decides for the space. The exec consults Breno only
  when another analysis is still needed or the scout returns a
  divergence/uncertainty.

  **Lifecycle versus resposta:** o `herdr agent prompt --wait` observa o
  lifecycle do pane e não acompanha a escrita do turno. Portanto `idle` ou
  `done` é apenas um candidato a término para dispatchers que esperam um
  arquivo. `herdr-ask` só assenta depois de ver um `answer.md` regular, não
  vazio e novo (ou alterado) desde o disparo; se o prazo acabar, registra
  `artifact_missing` e sai com falha, sem declarar uma resposta vazia como
  sucesso. O `metrics.json` é publicado por temporário no mesmo diretório e
  `os.replace`, para leitores concorrentes enxergarem sempre JSON completo.
  Os panes de `rev-1`, `rev-2` e `scout` são headless: texto residual na caixa
  de composição, inclusive uma letra digitada por engano, é descartável e não
  interrompe a cadeia. Um diálogo real continua bloqueando pelo canal oficial
  (`agent_blocked`); a proteção contra composição humana permanece no
  `-exec` e no `herdr-swap`.
  Antes de criar os prompts, o dispatcher revalida o snapshot de cada alvo sob
  um lock curto (status, pane, workspace, cwd, sessão, revision e
  `state_change_seq`). Se qualquer campo ou diálogo mudar, registra o
  `preflight_error` e não envia a rodada. Em um `agent_prompt_stalled`, a mesma
  checagem acontece antes de um retry: se o agent já ficou `working` ou a
  sequência avançou, usa `agent wait` e suprime o segundo prompt; só reenvia
  com a identidade e o estado idle/done inalterados.
  Se um papel obrigatório não estiver registrado, a consulta/revisão chama o
  bootstrap direcionado antes de criar a rodada e espera a restauração. A
  tabela fornece modelo/kind/raciocínio só quando não existe uma pane-base;
  o dispatcher não faz limpeza automática. Restauração e swap transferem o
  contexto por handoff, e qualquer divergência de perfil fica como evidência
  para decisão posterior.
- **`herdr-context-watch`** — leitura periódica (ou `--once`) de `rev-1`,
  `rev-2` e `scout` em `idle`/`done`. Extrai somente o bloco ancorado
  `Context window:` do status da CLI, registra modelo/reasoning como
  evidência, detecta mudanças de sessão durante a leitura e publica JSON
  atomicamente. Nunca envia prompt, `/clear`, `/new` ou troca de modelo.
- **`herdr-notify-watch`** — a `flock`-protected daemon that notifies via
  `omarchy-notification-send` when a `*-exec` agent has a genuine state
  change (edge-triggered on `state_change_seq`, not naive status polling).
- **`herdr-simulate-scout`** — prepara uma pergunta e evidências mínimas,
  roda a suíte e executa (ou apenas imprime com `--offline`) um smoke test da
  rota `herdr-ask --reviewer scout` em um space descartável. Exige um slug
  explícito e não libera slugs conhecidos de produção sem `--allow-production`.
- **`herdr-simulate-full.sh`** — encena o ciclo completo em um fixture de
  teste: duas rodadas com `rev-1`/`rev-2`, correção sintética, terceira análise
  automática pelo scout e registro da decisão pendente do Breno. Não faz commit
  nem altera arquivos do projeto.
- **`herdr-hydrate-execs`** — entrega aos `*-exec` já vivos a política de
  escalonamento atualizada. Pula `working`/`blocked` por segurança por padrão;
  `--include-working` é uma escolha explícita.

### Changing a space

- **`herdr-swap <slug> <role> <kind> [--dry-run]`** — swaps the kind
  (`claude`/`codex`/`grok`/etc) of a workspace's agent (`exec`, `rev-1`,
  `rev-2`, `scout`, ...), preserving context via a handoff file written by the
  outgoing agent. Everything stays reversible until the old pane is closed.
  `--dry-run` runs the real checks — agent state, migration gate, and the
  human-composition heuristic on the pane — then prints the plan and stops
  without touching anything. Worth running first: on 2026-09-12 it refused three
  of eleven execs because they had unsent human text sitting in the compose box.
- **`herdr-migrate-rev <slug>`** — renames a space's first reviewer from
  `<slug>-rev` to `<slug>-rev-1`. Not a swap: the kind, process, pane and cwd
  all stay: it is a `herdr agent rename` behind a lock, a durable
  `phase=migrating` marker, and a re-check under the lock. All spaces have
  already been through it — it stays for a space restored from an old backup.

- **`herdr-fix-layout [--dry-run] [--space <slug>]`** — restores tab 1 to
  `exec | rev-1 / rev-2`. Every agent restart closes a pane and splits another,
  and `pane split` can only place the new pane to the *right of* or *below* its
  target — it never rebuilds the column, so a batch of restarts typically ends
  with everything stacked in one column, or `rev-2` above `rev-1`. This broke
  three times on 2026-09-10, every time from restarts, every time spotted by the
  user rather than by tooling. It is not cosmetic: `herdr-fix-names` infers each
  pane's *role* from its position, so a broken layout makes the name repair aim
  at the wrong pane. The fix parks both reviewers in a temporary tab and brings
  them back in order — it cannot be done in place, because the moved pane always
  lands after the target, so the exec has to be the target and never the moved
  one. Idempotent: a correct space is reported `ok` and left alone.

- **`herdr-fix-names [--dry-run] [--space <label>]`** — repairs the `name` of
  agents Herdr has lost, matching each live pane to its expected role by
  position (exec is the left column, `rev-1` top-right, `rev-2` bottom-right,
  scout its own tab). Treat it as a stopgap: names repaired this way have been
  observed disappearing on their own. Measured 2026-09-09 across 41 agents,
  with no exception: 20/20 named agents had `interactive_ready=True`, 21/21
  unnamed ones had the field absent, and 15 names repaired by hand undid
  themselves within hours — only the ones created via `herdr agent start`
  survived.

  **Why that happens is not established.** An earlier version of this file
  claimed `herdr agent rename` "writes the name somewhere volatile"; that was
  **wrong**, and is corrected here. `~/.config/herdr/session.json` stores
  `agent_name` per pane, and names set by `rename` are in it — persistence is
  not the difference. The current hypothesis, untested, is that the binding is
  to the agent *session* rather than to the pane, so replacing the process
  (a `mise` auto-update was observed mid-session) drops it. Until that is
  reproduced, the script reports what is measurable and does not explain it:
  it warns when a reviewer/scout lacks `interactive_ready` (there the sturdier
  fix is `agent start`, which has empirically survived both a registry reset
  and a reboot), and for `-exec` rename is the only option anyway — restarting
  one would kill a session with a human behind it.

### Shared modules (not CLIs)

- **`_herdr_dispatch.py`** — mechanics behind `herdr-review-dispatch`,
  `herdr-ask` and `herdr-swap`: talking to the `herdr` CLI, resolving agent
  status/cwd, numbering a round directory, freezing context, canonicalising a
  project root, and retaining a legacy explicit reset helper alongside
  `dispatch_and_wait_all()` (concurrent `agent prompt --wait` per agent, with
  a per-agent watchdog for a genuinely sustained `blocked`). Também mantém a
  barreira opcional de artefato obrigatório e o escritor atômico de métricas;
  `herdr-ask` usa `answer.md` e `herdr-review-dispatch` usa `verdict.md`.
- **`_herdr_migration.py`** — the `<slug>-rev` → `<slug>-rev-1` rename
  mechanism and the per-space `.herdr/migration-state.json` (`phase`,
  `rev2_kind`). `herdr-migrate-rev` is the only writer; the dispatchers and
  the bootstrap only read it. Five review rounds (`herdr-4`..`herdr-8`)
  rejected earlier versions of this — the atomic lock, the write identity
  matching the attestation, and the re-check-after-lock all come from
  findings, not from design.

  Absence of the file means `phase=legacy`, which is correct for a space that
  predates the migration and still has a `<slug>-rev`. A **new** space never
  had one, so `herdr-add-space` writes `phase=migrated` before the bootstrap
  runs — without that, every space created today would be born needing a
  migration.

## `skills/`

Claude Code / Codex skills, shared through `~/.agents/skills/` (each
`SKILL.md` there is a symlink into this repo):

- **`herdr`** — general use of the Herdr CLI (inspecting/controlling panes,
  tabs, workspaces, agents).
- **`herdr-review`** — the dispatch protocol and finding classification
  (CONFIRMED/UNIQUE/CONFLICT) for the code-review cycle, capped at 2 rounds.
- **`herdr-ask`** — the dispatch protocol for open design questions, how to
  reconcile two positions by their premises, and how the automatic
  `--reviewer scout` third analysis returns uncertainty to the `-exec`.
- **`herdr-swap`** — the executor-swap protocol with context handoff.
  Antes de iniciar o sucessor, captura o perfil efetivo. Para Claude, a fonte
  usada para herdar é um `/status` confirmado com `Model` e `Effort` ancorados;
  o banner inicial pode estar stale depois de `/model` e nunca é usado sozinho.
  O handoff precisa ser reconhecido por `HERDR_HANDOFF_READ_OK` enquanto os dois
  panes ainda estão vivos. No modo de duas fases, a finalização revalida as
  identidades e roda a verificação antes de fechar o antigo; qualquer mudança
  deixa ambos preservados.

## `tests/`

```bash
python3 -m unittest discover -s tests -v
```

Most of these exist because something regressed, not because a checklist asked
for them:

- **`test_blocked_grace.py`** — the settle logic in
  `dispatch_and_wait_all()` (sustained-`blocked` vs. transient blip vs. real
  timeout). This loop has already regressed once by being "fixed" in a way
  that over-corrected.
- **`test_dispatch_artifact_lifecycle.py`** — impede que `done` feche uma
  rodada antes de `answer.md`/`verdict.md`, rejeita artefato velho, registra
  `artifact_missing` no prazo e verifica a publicação atômica de métricas.
- **`test_dispatch_preflight.py`** — revalida identidade sob lock antes de
  enviar, aborta quando `idle` vira `working` ou o alvo muda, e impede um
  segundo prompt no retry.
- **`test_agent_restore.py`** — cobre a recuperação automática de papéis
  ausentes, o lock contra duas restaurações concorrentes, o perfil pinado do
  HomeHub e a falha controlada quando o bootstrap não consegue recriar o papel.
- **`test_ask_request.py`** — a real bug caught while testing `herdr-ask`'s
  single-consultant mode: a placeholder string leaking into the isolation
  paragraph instead of being omitted.
- **`test_migration_state.py`** — phases, atomic writes, and the rule that a
  partial patch must not erase persisted non-default fields.
- **`test_dispatch_binding.py`**, **`test_role_attestation.py`**,
  **`test_herdr_env_gate.py`** — that a dispatch reaches the agent it claims
  to, that an agent's role is attested rather than inferred, and that the
  skills refuse to run outside a Herdr pane.

## Talking to another agent

`herdr agent prompt <target> "<text>"` — the CLI, nothing else. It delivers
through the supported path: it honours bracketed paste, sends an encoded Enter,
and refuses with `agent_blocked` when the target sits at a dialog, *before*
typing anything.

Two alternatives exist and are wrong for this:

- **The `herdr-mesh` MCP tools** (`herdr_relay`, `herdr_agent_send`,
  `herdr_handoff`) — a third-party package (`runchr-works/herdr-mesh`, no
  relation to this repo despite the name) that shells out to `herdr agent
  send`, a subcommand the 0.9.0 CLI does not have. Every call failed. Removed
  from this machine's Claude and Codex configs on 2026-09-10; nothing here ever
  used it, and all eleven binaries call the CLI directly.
- **`pane send-text` + `send-keys enter`** — types into the compose box. If a
  human has unsent text there, yours concatenates onto it and submits both. Two
  real incidents are recorded in the user's `CLAUDE.md`; `pane read` first
  narrows the window but does not close it, since someone can type between the
  read and the send.

## Testing against a throwaway environment

Herdr supports **named sessions**, each with its own socket and state, so
nothing here has to be tried against the working mesh:

```bash
herdr --session testlab server &          # headless server for that session
HERDR_SESSION=testlab herdr-agents        # this repo's scripts, isolated
herdr session delete testlab              # discard everything
```

**`HERDR_SESSION` is this repo's variable, not Herdr's.** `herdr --help`
documents only `HERDR_CONFIG_PATH`; the Herdr CLI isolates via the
`--session <name>` flag and ignores the environment entirely. Measured
2026-09-10: `HERDR_SESSION=testlab herdr agent list` returned the 41
**production** agents, while `herdr --session testlab agent list` returned 0.

So the variable only works for the scripts in this repo, which translate it
into the flag (`herdr_argv()` in `_herdr_dispatch.py`, one place every script
goes through). Calling the `herdr` CLI directly with the variable and no flag
runs against the real mesh — which is exactly how this bug hid: the README
used to teach the variable as isolation, and `herdr-add-space` printed
`MODO TESTE session=testlab` on seeing it, so an "isolated" add-space would
have created a workspace in the live mesh.

`HERDR_SESSION` isolates workspaces, panes and agents — but **not this repo's
files**. `herdr-add-space` would still write to the real `herdr-bootstrap`, so
point it elsewhere and give it a table with only the space under test;
otherwise the bootstrap will faithfully create *every* space in the table
inside your empty test session:

```bash
cp bin/herdr-bootstrap /tmp/bs   # then trim SPACES down to what you're testing
HERDR_SESSION=testlab HERDR_BOOTSTRAP_PATH=/tmp/bs herdr-add-space demo ~/some/dir
```

## Project context

Each real project using this cycle keeps its own `.herdr/reviewer.md`
(domain-specific invariants for the reviewers) — that lives in each project's
own repo, not here. `.herdr/review/` and `.herdr/ask/` hold the rounds
themselves and are deliberately not versioned.
