# herdr-mesh-tools

Personal scripts and skills built on top of [Herdr](https://github.com) (the
terminal multiplexer for coding agents) — this is not Herdr itself, it's the
automation layer on top of it: workspace/pane/agent bootstrap, background
notifications, swapping the executor's kind while preserving context, and a
blind/parallel review cycle between two independent reviewers.

The files here are the source of truth; `~/.local/bin/` and `~/.agents/skills/`
point back to this repo via symlink (the same pattern used for dotfiles).
Editing either location edits the same file.

## `bin/`

- **`herdr-bootstrap`** — ensures the workspaces/panes/agents of the working
  environment exist (idempotent). Defines, per workspace, who the executor is
  and who the two reviewers are (kind, model, effort), and arms a
  `herdr-notify-watch` for each `*-exec`.
- **`herdr-notify-watch`** — a `flock`-protected daemon that notifies via
  `omarchy-notification-send` when a `*-exec` agent has a genuine state
  change (edge-triggered on `state_change_seq`, not naive status polling).
- **`herdr-review-dispatch`** — fires a blind, parallel review round to the
  two reviewers of a workspace (`--verify` runs a cheaper single-reviewer
  verification pass). Freezes the diff (including untracked files), isolates
  each reviewer in its own subdirectory, and writes a per-round `metrics.json`
  for cost correlation.
- **`herdr-swap-exec`** — swaps the kind (`claude`/`codex`) of a workspace's
  `<slug>-exec` agent, preserving context via a handoff file written by the
  outgoing agent.

## `skills/`

Claude Code / Codex skills (shared via symlink into `~/.claude/skills/` and
`~/.codex/skills/`):

- **`herdr`** — general use of the Herdr CLI (inspecting/controlling panes,
  tabs, workspaces, agents).
- **`herdr-review`** — the dispatch protocol and finding classification
  (CONFIRMED/UNIQUE/CONFLICT) for the review cycle, capped at 2 rounds.
- **`herdr-swap`** — the executor-swap protocol with context handoff.

## `tests/`

Regression tests for the settle logic shared by `herdr-review-dispatch` and
`herdr-swap-exec` (sustained-`blocked` vs. transient blip vs. real timeout) —
this loop has already regressed once by being "fixed" in a way that
over-corrected, so the scenarios are checked mechanically instead of
re-derived by hand each time:

```bash
python3 -m unittest discover -s tests -v
```

## Project context

Each real project using this cycle keeps its own `.herdr/reviewer.md`
(domain-specific invariants for the reviewers) — that lives in each project's
own repo, not here.
