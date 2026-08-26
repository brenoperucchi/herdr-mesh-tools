# herdr-mesh-tools

Personal scripts and skills built on top of [Herdr](https://github.com) (the
terminal multiplexer for coding agents) — this is not Herdr itself, it's the
automation layer on top of it: workspace/pane/agent bootstrap, background
notifications, swapping the executor's kind while preserving context, and a
blind/parallel dispatch mechanism used for both code review and open design
questions between two independent reviewers.

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
- **`herdr-review-dispatch`** — fires a blind, parallel code-review round to
  the two reviewers of a workspace (`--verify` runs a cheaper single-reviewer
  verification pass). Freezes the diff (including untracked files), isolates
  each reviewer in its own subdirectory, and writes a per-round `metrics.json`
  for cost correlation.
- **`herdr-ask`** — same blind/parallel dispatch mechanism, for an open
  design question instead of a code diff (`--question-file`, optionally
  `--context <path>...` to freeze reference material, `--reviewer rev|rev-2`
  for a single cheaper consultant). The response format is a decision with
  explicit premises, not an atomic finding — writes to `.herdr/ask/`, a
  namespace separate from `.herdr/review/`.
- **`herdr-swap-exec`** — swaps the kind (`claude`/`codex`) of a workspace's
  `<slug>-exec` agent, preserving context via a handoff file written by the
  outgoing agent.
- **`_herdr_dispatch.py`** — shared mechanics behind `herdr-review-dispatch`,
  `herdr-ask`, and `herdr-swap-exec`: talking to the `herdr` CLI, resolving
  agent status/cwd, numbering a round directory, freezing context, and
  `dispatch_and_wait_all()` (concurrent `agent prompt --wait` per agent, with
  a per-agent watchdog for a genuinely sustained `blocked`). Not a CLI itself
  — imported by the other three via `sys.path`.

## `skills/`

Claude Code / Codex skills (shared via symlink into `~/.claude/skills/` and
`~/.codex/skills/`):

- **`herdr`** — general use of the Herdr CLI (inspecting/controlling panes,
  tabs, workspaces, agents).
- **`herdr-review`** — the dispatch protocol and finding classification
  (CONFIRMED/UNIQUE/CONFLICT) for the code-review cycle, capped at 2 rounds.
- **`herdr-ask`** — the dispatch protocol for open design questions, and how
  to reconcile two positions by their premises rather than by classifying
  findings.
- **`herdr-swap`** — the executor-swap protocol with context handoff.

## `tests/`

Regression tests for the settle logic in `_herdr_dispatch.dispatch_and_wait_all()`
(sustained-`blocked` vs. transient blip vs. real timeout) — this loop has
already regressed once by being "fixed" in a way that over-corrected, so the
scenarios are checked mechanically instead of re-derived by hand each time.
Also covers a real bug caught while testing `herdr-ask`'s single-consultant
mode (a placeholder string leaking into the isolation paragraph instead of
being omitted):

```bash
python3 -m unittest discover -s tests -v
```

## Project context

Each real project using this cycle keeps its own `.herdr/reviewer.md`
(domain-specific invariants for the reviewers) — that lives in each project's
own repo, not here.
