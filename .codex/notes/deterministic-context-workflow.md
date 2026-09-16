# Deterministic Codex Context Workflow

Codex must not rely on remembered workflow state for load-bearing claims. This
note is the source of truth for preventing context rot, drift, and hallucinated
closeout in Codex sessions for this repository.

## Operating Principle

Every substantive Codex arc materializes state from repository instruments at
defined gates. Memory, checkpoints, prior chat, and roadmap prose are
orientation only until re-grounded against HEAD.

The `just` recipes are the mandatory command surface. Direct
`tools/codex_context_guard.py` invocation is equivalent only when it uses the
same mode and flags.

## Required Gates

For speed-oriented command choices that preserve these gates, use
`.codex/notes/codex-workflow-optimization.md`.

### 1. Preflight

Run before substantive work:

```bash
just codex-preflight
```

The preflight materializes:

- repository root, cwd, branch, HEAD, and linked-worktree status
- dirty status and changed files
- `roadmap_status.md`'s recorded hash versus computed workspace hash
- open fork-doc count and latest retirement batch
- a local checkpoint artifact at `.harness/.checkpoints/codex-context-latest.json`

Hard failures stop work until resolved.

### 2. Edit Gate

All edits occur in isolated Codex worktrees. The root checkout is read/status
only. If the guard sees edits in the root checkout, the arc is invalid.

Do not mix design/spec/plan/fork-doc changes with implementation/test changes
unless the operator explicitly requested a design-phase/back-flow arc.

### 3. Cite Gate

For `C-*`, `U-*`, `H_T-*`, `ADR-*`, or CXA seam claims:

```bash
just overlay-query ...
```

Use `rg` for sibling `design-substrate/**` prose drift because the semantic
overlay intentionally does not scan sibling spec bodies.

### 4. Drift Recheck

After long work, merges, rebases, or context transition, rerun:

```bash
just codex-preflight
```

Treat memory/checkpoint "remaining work" as advisory until rechecked against
the current roadmap_status.md, git state, and source files.

For an explicit mid-arc checkpoint:

```bash
just codex-checkpoint mid-arc
```

The checkpoint records the current context fingerprint, HEAD, branch, changed
files, status entries, roadmap_status.md state, and findings. It is ignored by
git and exists to make context-refresh moments inspectable rather than
remembered.

### 5. Closeout

Run before final response, commit, or PR:

```bash
just codex-closeout
```

Closeout checks:

- worktree-only edit discipline
- design/implementation boundary
- `roadmap_status.md` hash drift on the default branch
- cite-bearing changes that require `just overlay-check`
- missing tracking-surface review
- fresh checkpoint match against current HEAD/status/roadmap
- active autonomous-loop state, when present, has reached every pre-closeout
  gate from linked worktree readiness through decorrelated review

The closeout recipe first writes a `pre-closeout` checkpoint, then runs the
closeout guard with `--require-fresh-checkpoint`. A stale or missing checkpoint
is a hard failure when freshness is required.

### 5a. Autonomous Loop State

For autonomous coding arcs, initialize a local evidence ledger:

```bash
just codex-autonomous-arc <arc-id>
```

Record gates as the controller/coder/validator/GitHub-shipping loop advances:

```bash
just codex-loop-record --phase plan --status passed --command "..." --evidence "..."
just codex-loop-record --phase red --status failed --command "..." --evidence "..."
just codex-loop-record --phase implementation --status passed --command "..." --evidence "..."
```

The required sequence is:

```text
worktree_ready -> preflight -> plan -> red(status=failed) -> implementation -> narrow_verify -> local_gate -> decorrelated_review -> closeout -> commit -> push -> pr_opened -> ci_green -> merged -> post_merge_refresh -> main_synced -> worktree_disposition
```

The state file is `.harness/codex_loop_state.json`; it is gitignored because it
is per-run state, not a project artifact. `just codex-closeout` checks active
loop state through `decorrelated_review`; record the `closeout` phase after the
closeout command succeeds. Then commit, push, open the PR, watch CI, merge,
perform any owed terminating refresh, sync local main, and record worktree
disposition. The final disposition is a hygiene gate: copy the gitignored loop
state to synced `main`, remove the original arc worktree, prune the local topic
branch, then record `worktree_disposition`. Run `just codex-loop-check` only
when the full lifecycle has been recorded. Loop records include branch, HEAD,
linked-worktree status, and a worktree fingerprint; changes after
`implementation` or any later pre-commit gate require re-recording that gate
and all downstream pre-commit gates before committing.

### 6. Credential Gates

Credential-gated units are not skipped. Codex drives the unit as far as it can
without credential material or paid-provider execution:

1. build the stdlib/mockable/provider-free slice
2. run the narrow verification that proves non-credential work is closed
3. stop at the exact credential or paid-provider gate
4. use an available HIL/operator-approval surface when one exists
5. when no HIL surface is available, log the gate for human review

Log the gate with:

```bash
just codex-credential-gate --unit R-NNN \
  --gate "OPENAI_API_KEY required for live mixed-provider e2e" \
  --forward-closed "provider-free tests passed; only live provider call remains" \
  --resume "ask operator for OPENAI_API_KEY authorization, then run the live e2e" \
  --command "OPENAI_API_KEY=<name-only> uv run pytest ..."
```

The command appends `.harness/codex_credential_gates.jsonl`, redacting
secret-like `NAME=value` fragments before writing. The ledger records only gate
metadata, never credential values.

After logging a credential gate, update a human-facing tracking surface
(`Project_Roadmap_v1.md` or `.harness/roadmap_status.md`) so the pending gate is
visible the next time a human engages with Codex. Closeout hard-fails if the
credential ledger changed without that tracking update. Once the gate is logged
and all non-credential forward actions are proven closed, Codex proceeds to the
next implementable unit instead of parking the session.

### 7. Tracking Surface Audit

No substantive task is complete until required tracking surfaces are updated or
explicitly reported as not applicable:

- `Project_Roadmap_v1.md`
- `.harness/roadmap_status.md`
- `.harness/substitutions.yaml`
- retirement batches under `.harness/phase-7d-retirement-events-batch-*.md`
- credential gates under `.harness/codex_credential_gates.jsonl`
- fork docs under `.harness/class_*_fork_*.md`
- axis `CLAUDE.md` / `AGENTS.md` files when posture changes
- clearance markers for design/spec/plan amendments
- memory entries when a pattern reaches the memory threshold

The PR body or final response must report implementation status, verification,
tracking updates, and any owed follow-on refresh.

## Tool Contract

`tools/codex_context_guard.py` is the deterministic checker. It has three modes:

```bash
just codex-preflight
just codex-checkpoint <label>
just codex-credential-gate --unit ... --gate ... --forward-closed ... --resume ...
just codex-closeout
just codex-context-check
```

Related Codex-local optimization commands:

```bash
just codex-worktree-gc          # dry-run safe stale-worktree cleanup
just codex-worktree-gc --reap   # explicit mutex/lease-safe removal after merge/closeout
just codex-test                 # provider-free non-e2e pytest lane
just codex-check                # sync + lint + typecheck + provider-free non-e2e pytest
just codex-autonomous-arc       # initialize autonomous-loop evidence state
just codex-loop-record ...      # append one controller/coder/validator/GitHub-shipping gate
just codex-loop-check           # verify all autonomous-loop gates, including GitHub shipping and worktree disposition
just coderabbit-review ...      # optional advisory CodeRabbit review
```

`codex-context-check` is the combined hard gate for local validation. It exits
nonzero on hard violations and requires a fresh checkpoint. The local closeout
and context-check recipes pass `--include-branch-diff`, so a clean feature
worktree is still checked against committed changes since the merge-base with
the default branch.

`codex-context-check-ci` runs the guard the way CI does: `check` with explicit
`--base-ref` / `--head-ref` set to the SHAs of `origin/main` and `HEAD`, each read
once, and `--allow-roadmap-drift`, no checkpoint. It refuses a branch that does not contain `origin/main`, because CI's
two-endpoint diff would then include main's newer commits; fetch and rebase first.
It is the Claude lane's pre-push step (C-HE-33). The Codex ship flow does not run it:
inside an active Codex autonomous loop the pre-closeout gates are bound to the
pre-commit HEAD, so `check` reports `CODEX_LOOP_INCOMPLETE` from the commit until
every ship gate is recorded, a local-only finding CI never sees. The findings the two local
recipes may legitimately disagree on are named in
`tools/test_codex_context_guard.py::test_local_ci_parity`.

CI runs the guard directly without local checkpoint freshness because
`.harness/.checkpoints/` is intentionally untracked. The CI invocation passes
explicit `--base-ref` / `--head-ref` values from the GitHub event so the guard
checks the committed PR range instead of an empty clean-checkout status.
`--allow-roadmap-drift` downgrades non-default-branch drift unconditionally,
and on the default branch it downgrades ONLY the one-commit "owed lag" case
(HEAD's parent, not HEAD itself, is a verified terminating refresh — the
post-merge CI push scenario). It cannot mask arbitrary default-branch
roadmap_status.md drift (two-or-more-commit accumulated drift still hard-fails
even with the flag). HEAD itself being the verified refresh is tolerated
unconditionally, for every caller, flag or no flag — see `_lag_expected()` vs
`_owed_lag()` in `tools/codex_context_guard.py`. When `gh pr list` is
unavailable, the guard emits `OPEN_PRS_UNAVAILABLE` instead of silently
treating the open-PR set as authoritative.

The Codex `SessionStart` and `Stop` hooks invoke the same guard. Hook failures
propagate nonzero when the guard cannot run or reports a hard finding, except that
`CODEX_LOOP_INCOMPLETE` alone is advisory at `Stop` while the explicit
`just codex-closeout` completion/commit/PR gate remains hard.
