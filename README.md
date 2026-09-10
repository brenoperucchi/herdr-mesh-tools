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
| `<slug>-rev-1` | 1 | first reviewer (Codex, `model_reasoning_effort=high`) |
| `<slug>-rev-2` | 1 | second reviewer (Claude Opus) — deliberately a different lens |
| `<slug>-scout` | 2 | parallel reading/exploration; its own tab because the work is a different kind |

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
  workspace, who the executor is and who the two reviewers are (kind, model,
  effort), and arms a `herdr-notify-watch` for each `*-exec`. Run it from
  anywhere: it reads absolute paths from its own table, not the cwd.
  `--dry-run` first — it has already caught an entry gone stale after a
  workspace was renamed by hand, which would have created a duplicate.
- **`herdr-agents [--name <cwd-filter>]`** — one tabular line per agent: status,
  cwd, kind, **model**, **effort**, name, pane, tab. Model and effort come from
  the process's real `argv` (`herdr pane process-info`), not from the expected
  config — the point is to see divergence. A value in **(parentheses) means
  inherited**, not pinned at launch: the process came up without
  `--model`/`--effort` and is following `~/.codex/config.toml` or
  `~/.claude/settings.json`. That distinction matters because the interactive
  `/model` command rewrites those files, so one `/model` in any pane silently
  changes every inherited agent — which is how two reviewers ended up on Fable
  5.1, the priciest model in the catalogue, with nobody having decided it.

### Running the cycle

- **`herdr-review-dispatch`** — fires a blind, parallel code-review round to
  the two reviewers of a workspace (`--verify` runs a cheaper single-reviewer
  verification pass). Freezes the diff (including untracked files), isolates
  each reviewer in its own subdirectory, and writes a per-round `metrics.json`
  for cost correlation.
- **`herdr-ask`** — same blind/parallel dispatch mechanism, for an open
  design question instead of a code diff (`--question-file`, optionally
  `--context <path>...` to freeze reference material, `--reviewer
  rev|rev-1|rev-2` for a single cheaper consultant — `rev` and `rev-1` are
  aliases of the same role). The response format is a decision with explicit
  premises, not an atomic finding — writes to `.herdr/ask/`, a namespace
  separate from `.herdr/review/`.
- **`herdr-notify-watch`** — a `flock`-protected daemon that notifies via
  `omarchy-notification-send` when a `*-exec` agent has a genuine state
  change (edge-triggered on `state_change_seq`, not naive status polling).

### Changing a space

- **`herdr-swap <slug> <role> <kind>`** — swaps the kind
  (`claude`/`codex`/`grok`/etc) of a workspace's agent (`exec`, `rev-1`,
  `rev-2`, `scout`, ...), preserving context via a handoff file written by the
  outgoing agent. Everything stays reversible until the old pane is closed.
- **`herdr-migrate-rev <slug>`** — renames a space's first reviewer from
  `<slug>-rev` to `<slug>-rev-1`. Not a swap: the kind, process, pane and cwd
  all stay: it is a `herdr agent rename` behind a lock, a durable
  `phase=migrating` marker, and a re-check under the lock. All spaces have
  already been through it — it stays for a space restored from an old backup.

- **`herdr-fix-names [--dry-run] [--space <label>]`** — repairs the `name` of
  agents Herdr has lost, matching each live pane to its expected role by
  position (exec is the left column, `rev-1` top-right, `rev-2` bottom-right,
  scout its own tab). Deliberately a stopgap: `name` and `interactive_ready`
  are written together by the *durable* path, which is `herdr agent start` —
  `herdr agent rename` writes the name somewhere volatile, so a repaired name
  disappears at the next registry reset. Measured 2026-09-09 across 41 agents
  with no exception: 20/20 named agents had `interactive_ready=True`, 21/21
  unnamed ones had the field absent, and 15 names repaired by hand undid
  themselves within hours. So the script *says so*: it warns when a
  reviewer/scout lacks the durable registration (there the real fix is
  `agent start`), and only for `-exec` is rename the sole option — restarting
  one would kill a session with a human behind it.

### Shared modules (not CLIs)

- **`_herdr_dispatch.py`** — mechanics behind `herdr-review-dispatch`,
  `herdr-ask` and `herdr-swap`: talking to the `herdr` CLI, resolving agent
  status/cwd, numbering a round directory, freezing context, canonicalising a
  project root, and `dispatch_and_wait_all()` (concurrent `agent prompt
  --wait` per agent, with a per-agent watchdog for a genuinely sustained
  `blocked`).
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
- **`herdr-ask`** — the dispatch protocol for open design questions, and how
  to reconcile two positions by their premises rather than by classifying
  findings.
- **`herdr-swap`** — the executor-swap protocol with context handoff.

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
- **`test_ask_request.py`** — a real bug caught while testing `herdr-ask`'s
  single-consultant mode: a placeholder string leaking into the isolation
  paragraph instead of being omitted.
- **`test_migration_state.py`** — phases, atomic writes, and the rule that a
  partial patch must not erase persisted non-default fields.
- **`test_dispatch_binding.py`**, **`test_role_attestation.py`**,
  **`test_herdr_env_gate.py`** — that a dispatch reaches the agent it claims
  to, that an agent's role is attested rather than inferred, and that the
  skills refuse to run outside a Herdr pane.

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
