# justfile — operator command surface for the H_T workspace.
#
# Recipes that exercise real LLM / multi-process orchestration load secrets
# from a repo-local .env (gitignored). Create it with:
#
#     cp .env.example .env && chmod 600 .env
#     # then edit .env to set ANTHROPIC_API_KEY, etc.
#
# `just --list` to see all recipes.

set dotenv-load := true
set dotenv-required := false
set positional-arguments := true
export UV_CACHE_DIR := env_var_or_default("UV_CACHE_DIR", "/tmp/arhugula-uv-cache")

# Default recipe: list everything.
default:
    @just --list

# ─── core dev loop ─────────────────────────────────────────────────────────

# Run the default provider-free pytest suite, matching CI's blocking test lane.
test:
    env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u E2B_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_CLOUD_PROJECT PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run pytest -m "not e2e"

# Run a single test file or node id. Example: just test-one harness-cp/tests/test_foo.py
test-one *args:
    uv run pytest {{args}}

# Pyright type-check across the workspace.
typecheck:
    uv run pyright

# Ruff lint.
lint:
    uv run ruff check .

# Ruff format.
fmt:
    uv run ruff format .

# Check Ruff formatting without changing files.
fmt-check:
    uv run ruff format --check .

# Full pre-merge gate: workspace sync + lint + typecheck + docs/closure + provider-free tests.
check: codex-sync lint fmt-check typecheck docs-completeness-check memory-closeout-check closure-certification-check clearance-parse-check artifact-heads-check test

# Codex provider-free pytest lane. Strips live provider env and mirrors CI's non-e2e gate.
codex-test *args:
    env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u E2B_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_CLOUD_PROJECT PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run pytest -m "not e2e" {{args}}

# Synchronize all workspace packages before the Codex PR-ready gate.
codex-sync:
    uv sync --all-packages

# Blocking provider-free regression lane for Codex hooks, permissions, lifecycle, and GC.
codex-parity-check:
    bash tools/codex-parity-check.sh

# Exercise hook dispatch through the installed Codex CLI using only a loopback model double.
codex-hook-runtime-witness:
    /usr/bin/python3 tools/codex_hook_runtime_witness.py

# Codex PR-ready local gate without live provider credentials.
codex-check: codex-sync lint fmt-check typecheck docs-completeness-check memory-closeout-check closure-certification-check clearance-parse-check artifact-heads-check codex-parity-check
    env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u E2B_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_CLOUD_PROJECT PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run pytest -m "not e2e"

# ─── Codex deterministic context guard ─────────────────────────────────────

# Materialize repo/worktree/roadmap state before substantive Codex work.
codex-preflight:
    /usr/bin/python3 tools/codex_context_guard.py preflight

# Write a deterministic local context checkpoint.
codex-checkpoint label="manual":
    /usr/bin/python3 tools/codex_context_guard.py checkpoint --label {{label}}

# Materialize closeout obligations before final response, commit, or PR.
codex-closeout:
    /usr/bin/python3 tools/codex_context_guard.py checkpoint --label pre-closeout --include-branch-diff
    /usr/bin/python3 tools/codex_context_guard.py closeout --require-fresh-checkpoint --include-branch-diff

# Combined local hard gate for context rot / drift / tracking omissions.
codex-context-check:
    /usr/bin/python3 tools/codex_context_guard.py checkpoint --label local-check --include-branch-diff
    /usr/bin/python3 tools/codex_context_guard.py check --require-fresh-checkpoint --include-branch-diff

# Log a credential-gated unit after all non-credential work is closed.
codex-credential-gate *args:
    /usr/bin/python3 tools/codex_context_guard.py credential-gate {{args}}

# Start the deterministic Codex autonomous loop ledger for this arc.
codex-loop-start arc="manual":
    /usr/bin/python3 tools/codex_loop.py start --arc {{arc}}

# Record one autonomous-loop gate. Example:
# just codex-loop-record --phase red --status failed --command "uv run pytest ..." --evidence "expected assertion"
codex-loop-record *args:
    /usr/bin/python3 tools/codex_loop.py record "$@"

# Show current autonomous-loop gate status.
codex-loop-status:
    /usr/bin/python3 tools/codex_loop.py status

# Fail until worktree, development, PR, CI, merge, main-sync, and worktree-disposition gates are recorded.
codex-loop-check:
    /usr/bin/python3 tools/codex_loop.py check

# Bootstrap a Codex autonomous implementation arc. The agent still performs the
# coding/review steps; this recipe creates the evidence ledger and prints the gate order.
codex-autonomous-arc arc="manual":
    /usr/bin/python3 tools/codex_loop.py start --arc {{arc}}
    /usr/bin/python3 tools/codex_loop.py record --phase worktree_ready --status passed --command "git worktree ready" --evidence "linked worktree confirmed for autonomous arc"
    just codex-preflight
    /usr/bin/python3 tools/codex_loop.py record --phase preflight --status passed --command "just codex-preflight" --evidence "preflight completed and checkpoint written"
    @echo "Next gates: worktree_ready -> preflight -> plan -> red(status=failed) -> implementation -> narrow_verify -> local_gate -> decorrelated_review -> closeout -> commit -> push -> pr_opened -> ci_green -> merged -> post_merge_refresh -> main_synced -> worktree_disposition"

# Dry-run safe stale-worktree cleanup. Use --reap to remove only clean merged candidates.
codex-worktree-gc *args:
    /usr/bin/python3 tools/codex_worktree_gc.py {{args}}

# ─── semantic overlay (R-IF-112) — spec ↔ code ↔ CXA-seam ↔ substitution ────
# A deterministic, no-LLM overlay over the code graph. The agent-facing reference
# tool: "what code implements C-IS-08", "who cites U-RT-112", "show me the orphans".

# Human-readable counts + orphan report (always fresh from HEAD).
overlay:
    uv run python tools/semantic_overlay/overlay.py summary

# Re-derive + write the gitignored overlay.json artifact (the dashboard/enrich consumer).
overlay-build:
    uv run python tools/semantic_overlay/overlay.py build

# CI gate: HARD drift = a CXA seam whose producer/consumer symbol no longer resolves,
# or a stale tracked overlay.json. Exit 1 on either.
overlay-check:
    uv run python tools/semantic_overlay/overlay.py check

# Agent reference lookup, e.g.: just overlay-query --contract C-IS-08
overlay-query *ARGS:
    uv run python tools/semantic_overlay/overlay.py query "$@"

# ─── graft reachability — the overlay's reachability-side sibling ───────────
# The overlay holds no call edges, so it cannot answer "does a production path reach
# this, or do only the tests touch it?" — the wired-handler-unreachable shape. Reads
# graft's wiring graph, so it needs `graft build` in this checkout; it fails loud
# (exit 2) rather than reporting an empty OK when the graph is absent. Advisory:
# findings never fail the run. NOT a CI gate — the graph is gitignored/per-checkout.

# src symbols whose only inbound callers are tests (needs `graft build`; --all, --json)
reachability *ARGS:
    uv run python tools/graft_reachability.py "$@"

# R-CTX-1 context-budget instrument: first-turn preload per session (median/mean),
# plus --post-compaction (errata E4 selector) and --sidechains views.
# e.g.: just context-budget --sessions 20 --post-compaction
context-budget *ARGS:
    uv run python tools/context_budget.py "$@"

# ─── clearance corpus (R-CTX-1 / U-CTX-10) — marker frontmatter parse gate ──
# Fail-closed over .harness/clearance/*.md: every marker's YAML frontmatter must
# parse to a mapping carrying artifact + version AND carry no lossy scalar (the
# whitespace-`#` value that parses and then silently truncates). The only
# exemptions are the rows explicitly enumerated in
# .harness/clearance/parse-manifest.md — an unlisted unparseable file is an
# ERROR, never a skip.
clearance-parse-check:
    uv run python tools/clearance_frontmatter.py --check

# Quote-only repair of broken/lossy marker scalars (no content is reworded).
clearance-parse-fix:
    uv run python tools/clearance_frontmatter.py --fix

# ─── artifact heads (R-CTX-1 / U-CTX-11) — per-family head versions ─────────
# Derives .harness/artifact-heads.md from the clearance corpus (the version-
# binding record per root CLAUDE.md §4.5) instead of hand-maintained prose.
# Fail-closed: an unparseable marker aborts the derivation, never a skipped row.
# Two gates: generated-vs-committed, and marker-completeness.
artifact-heads-check:
    uv run python tools/artifact_heads.py --check
    uv run python tools/artifact_heads.py --check-completeness

# Regenerate the committed table after a new clearance marker lands.
artifact-heads-write:
    uv run python tools/artifact_heads.py --write

# Print the derived table without touching the committed one.
artifact-heads:
    uv run python tools/artifact_heads.py --print

# ─── forward register — structured post-Phase-8 B-* forward-work schema ─────
# Sibling to arc-ledger.yaml (see tools/forward_register.py's own header for why
# arc-ledger.yaml itself cannot carry these rows). Prose home stays at
# .harness/post-phase-8-forward-register.md; this is the queryable summary layer.
forward-register *ARGS:
    uv run python tools/forward_register.py "$@"

# CI gate: impossible/stale tally or prose-heading drift = exit 1.
forward-register-check:
    uv run python tools/forward_register.py --check

# ─── per-ROUND arc self-check (the pre-push global-property gate) ───────────
# Re-resolves every file:line cite the arc ADDED, cross-checks count claims
# against every mirror, scans newly-minted §labels for delta-chain collisions,
# and asserts a touched register row renders a prose body under --detail.
# Run it BEFORE EVERY PUSH, not once per arc — the B-71 leg's rounds 7-10 found
# defects the ABSORPTION rounds introduced, which a per-arc check cannot see.
# See tools/leg_selfcheck.py's own header for the diagnosis.
#   just leg-selfcheck --uncommitted   # before committing
leg-selfcheck *ARGS:
    uv run python tools/leg_selfcheck.py "$@"

# ─── roadmap_status.md — deterministic mechanical-skeleton refresh ──────────
# Owns the anchor table / in-flight PR table / capped recently-completed +
# drift-log tables. Does NOT touch the agent-authored Next-action prose.
# See tools/roadmap_status_refresh.py's own header.
roadmap-status *ARGS:
    uv run python tools/roadmap_status_refresh.py "$@"

# CI/pre-commit gate: cap violations or hash mismatch = exit 1.
roadmap-status-check:
    uv run python tools/roadmap_status_refresh.py --check

# ─── arc-metrics ledger — wall-clock efficacy tracking (B-170) ──────────────
# One row per arc at .harness/arc-metrics.jsonl. The load-bearing field is
# levers_active: each arc records which wall-clock levers were live when it ran,
# so efficacy is a COHORT COMPARISON, never an assertion.
# Fields carry provenance (derived / declared / unmapped) — an absent measurement
# is never recorded as a measured zero. Summary reports medians WITH RANGE:
# measured round variance is ~5x, so a bare mean misleads.
# Capture is split across two arcs so no topic worktree is ever left dirty:
# `queue` (at arc closure) writes only OUTSIDE the repo, `drain` (early in the
# next arc) folds the queued rows into the tracked ledger inside that arc's PR.
#   just arc-metrics queue --pr 1340 --arc-type applying --decisions 1
#   just arc-metrics drain
#   just arc-metrics summary
arc-metrics *ARGS:
    uv run python tools/arc_metrics.py "$@"

# ─── arc LEVER REPORT — skill-lever cohorts vs untreated baseline (B-211/B-212) ─────
# Read-only observability: treated arcs (levers_active declares a target lever id)
# against the empty-lever baseline, medians + per-arc deltas. Reading rules live at
# .claude/skills/arc-lever-report/SKILL.md — a bare number dump is not a report.
#   just arc-lever-report --arc-type applying
#   just arc-lever-report --json
arc-lever-report *ARGS:
    uv run python tools/arc_lever_report.py "$@"

# ─── arc COST — per-arc transcript cost extractor (U-HE-48, C-HE-25 X6e) ────
# requestId-deduplicated usage (naive sums double-count ~1.9x) ranked by the IET
# index; subagent transcripts included; stage windows cut at transcript event
# timestamps via --cut. Feeds the arc row via `arc-metrics queue --transcript`.
#   just arc-cost ~/.claude/projects/<proj>/<session>.jsonl --cut 2026-08-26T21:16:18Z
arc-cost *ARGS:
    uv run python tools/arc_cost.py "$@"

# ─── arc EXIT REPORT — machine-readable arc-closure record (U-WT-03) ────────
# Run as the FINAL ship-pr step, AFTER the reflect / context-save-lean block: only there do
# the merge SHA, post-merge main-CI conclusion, terminating-refresh commit and the
# just-written checkpoint all exist. Writes the gitignored
# .harness/.checkpoints/arc-exit-report-pr<NNN>.md (PR-keyed, overwritten on re-run) and
# indexes one EXIT-REPORT row into the shared loop_status.md (C-HE-09 §2 venue).
#   just arc-exit-report --pr 1202 --merge-sha 995517e5
arc-exit-report *ARGS:
    uv run python tools/arc_exit_report.py "$@"

# ─── arc CLOSE — the session-independent close-out tail in ONE call (B-230 Task 3) ──
# Runs `arc-exit-report` then `arc-metrics queue`; `just` stops at the first non-zero
# exit. Everything after the three positionals is forwarded VERBATIM to
# `arc-metrics queue`, whose contract is unchanged: omit --transcript when no transcript
# matches unambiguously, --levers is zero or many separate tokens. Recipe lines run
# under `sh -cu` (positional-arguments, no `shell` set): POSIX only — `shift 3` then
# `"$@"`, never `${@:4}`; each line is its own shell, so a shift is local to its line.
# The tail is checked BEFORE anything runs: `--pr` is bound to the first positional, and
# a second `--pr` in the tail would let argparse's last-wins write the exit report for
# one PR and queue metrics for another (codex r1 on b-230-task-3).
#   just arc-close 1503 b897542dc .harness/.checkpoints/<cp>.md --arc-id b-230-task-1 \
#     --arc-type applying --decisions 0 --round-logs .harness/tmp/b-230-task-1-rounds/r1.log
arc-close pr merge_sha checkpoint *QUEUE_ARGS:
    shift 3; for a; do case "$a" in --pr|--pr=*) echo "arc-close: --pr is bound to the first positional; drop it from the queue tail" >&2; exit 2;; esac; done
    just arc-exit-report --pr "$1" --merge-sha "$2" --checkpoint "$3"
    pr="$1"; shift 3; just arc-metrics queue --pr "$pr" "$@"

# ─── branch hygiene batch — ONE guarded push for every deferred deletion (B-230 Task 4) ──
# Phase 1 reads the pending-HIL reducer (last-write-wins per item; the shared ledger at
# `loop_status_path()`), verifies every branch against its PR, and prints one `--atomic`
# force-with-lease push for the operator to PASTE in an interactive session — the guard
# denies force pushes to the loop by design; this collapses N approvals into one. One
# mismatch aborts the batch with nothing on stdout. Phase 2 appends the RESOLVED-HIL row
# for each item whose branches are gone on origin (ls-remote exit 2, nothing else), which
# is what stops the reducer re-presenting them; rerunnable — it reports `still present:`
# and never re-issues a push. `bash -o pipefail`: an unreadable ledger must fail the
# recipe, not read as an empty queue.
branch-hygiene-pending:
    bash -o pipefail -c 'source tools/hooks/lib.sh; source tools/hooks/loop_lib.sh; loop_pending_hil_list | uv run python tools/branch_hygiene_batch.py --pending - --emit-command'

branch-hygiene-resolve:
    bash -o pipefail -c 'source tools/hooks/lib.sh; source tools/hooks/loop_lib.sh; loop_pending_hil_list | uv run python tools/branch_hygiene_batch.py --pending - --resolve'

# ─── mutation probe — prove a test PINS named source lines (U-WT-06) ────────
# Comments the range out, re-runs the test, and restores from memory (never git
# stash / git checkout). Refuses a dirty file, an already-red test, or a range whose
# removal breaks syntax. Exit 0 pinned / 1 PROBE FAILED (test stayed green) /
# 2 refused-or-indeterminate / 3 restore/release failure (file may be mutated, or restored with the sidecar retained). THIS WRITES SOURCE FILES — read
# tools/mutation_probe.py's header before using it.
#   just mutation-probe --file tools/hooks/postedit-lint.sh --lines 34 \
#     --test "bash tools/hooks/test_postedit_lint.sh"
mutation-probe *ARGS:
    uv run python tools/mutation_probe.py "$@"

# ─── §8.1 verification manifest (spec-he-loop-lanes, U-HE-05) ───────────────
# tools/lanes_verify.py owns the manifest as data; rows are appended by the unit
# that lands each artifact. `lanes-verify` runs every row.
lanes-verify:
    uv run python tools/lanes_verify.py verify

# Phase-0 gate: every phase0 row must PASS at HEAD; a skip counts as NOT passed
# (C-HE-13 §1). Consumed by the mechanical pilot gate.
lanes-phase0-check:
    uv run python tools/lanes_verify.py phase0

# C-HE-13 §1: refuse to start a pilot unless every phase0 row PASSES at HEAD (a skip is
# NOT a pass), then print the manual N-lane recipe. Read-only; starts nothing itself.
lanes-pilot run_id:
    uv run python tools/lanes_pilot.py gate && uv run python tools/lanes_pilot.py start {{run_id}}

# C-HE-13 §3: the success iff-clause across the three stores. Exit 1 = FAIL, 2 = unanswerable.
lanes-pilot-report run_id:
    uv run python tools/lanes_pilot.py report {{run_id}}

# Every manifest row marked mutation-probe must have a PINNED result in
# .harness/mutation-probe-log.jsonl (spec §0.3; the probe tool appends it).
mutation-probe-coverage-check:
    uv run python tools/lanes_verify.py coverage

# C-HE-22 / C-HE-13 §2 (U-HE-35 codex r10): fail-closed pilot admission — pilots may
# start only when the gate log carries a GREEN probe-result row from the live
# reviewer-concurrency probe. Absent (never run) and RED both refuse.
pilot-gate-check:
    uv run python tools/lanes_verify.py pilot-gate

# ─── C-HE-08 branch protection for main (server-side X9 fence; operator-gated apply) ──────
# `apply` shows the diff and MUTATES NOTHING; the operator approves the actual payload
# (AskUserQuestion), then `apply-confirm` performs the provisional apply + tiebreaker
# (+ automatic rollback on FAIL). Never hard-code --confirm.
main-protection-show:
    uv run python tools/main_protection.py show
main-protection-apply:
    uv run python tools/main_protection.py apply
main-protection-apply-confirm digest:
    uv run python tools/main_protection.py apply --confirm --approved-digest {{digest}}
main-protection-rollback:
    uv run python tools/main_protection.py rollback
main-protection-verify:
    uv run python tools/main_protection.py verify
main-protection-tiebreaker:
    uv run python tools/main_protection.py tiebreaker

# C-HE-06 §6: clear a `blocked` merge-door lease -- operator-confirmed reclaim through the marker CAS,
# keyed to the blocked SHA. There is NO raw-unlink recipe by design. The lane id falls back to the
# persisted .harness/.lane-id (the door's emitted recovery command carries no env prefix, and shell
# exports do not survive across Bash tool calls); an empty lane would mint an unresumable successor
# lease, so absence of BOTH sources aborts loud (U-HE-28 codex r1+r9).
merge-door-unblock pr sha='':
    #!/usr/bin/env bash
    set -euo pipefail
    lane="${HARNESS_LANE_ID:-$(cat .harness/.lane-id 2>/dev/null || true)}"
    [ -n "$lane" ] || { echo "merge-door-unblock: no HARNESS_LANE_ID and no .harness/.lane-id (run lane-init)" >&2; exit 64; }
    # C-HE-06 §6 canonical ONE-arg form (U-HE-28 codex r18): with no sha, read the
    # blocked lease's own blocked_at_sha from door state and confirm it. The explicit
    # two-arg form stays for the DEFERRED-HIL rows that name both.
    sha="{{sha}}"
    if [ -z "$sha" ]; then
      sha="$(uv run python tools/merge_door.py status | uv run python -c 'import json,sys; d=json.load(sys.stdin) or {}; print(d.get("blocked_at_sha") or "")')"
      [ -n "$sha" ] || { echo "merge-door-unblock: no blocked lease (or no blocked_at_sha) in door state" >&2; exit 65; }
      echo "merge-door-unblock: using blocked_at_sha=$sha from door state" >&2
    fi
    uv run python tools/merge_door.py unblock {{pr}} "$sha" --lane-id "$lane"
merge-door-status:
    uv run python tools/merge_door.py status

# C-HE-23 §2 consistency reducer over merge-gate-log.md <-> .jsonl (U-HE-13).
# Exit 1 on a markdown row with no JSONL sibling; orphans are listed and
# reconciled by `uv run python tools/merge_gate_log.py reconcile`.
merge-gate-log-check:
    uv run python tools/merge_gate_log.py check

# Publish one merge-gate lens's six binding values (C-HE-15 §4) to a file and print ONLY
# that path (U-SR-03, charter WR-09). Name the printed path in the lens prompt and let the
# lens read it -- never copy the values through the orchestrator, which is where both
# round-3 corruptions came from.
merge-gate-binding lens base='main':
    uv run python tools/merge_gate_log.py binding --lens {{lens}} --base {{base}}

# Record one lens verdict: JSONL first, markdown second (C-HE-23 §2, U-HE-13). Exit 0 APPROVE
# recorded / 1 BLOCK recorded / 2 NOT recorded (does not count; re-run the lens).
#   just merge-gate-emit --pr <N> --arc-id <arc-id> --lens merge-gate-<id> --verdict-json .harness/tmp/<file>
#   (--arc-id is the RESERVATION id, e.g. u-he-34 -- omitting it defaults the row to
#   pr-<N>, which breaks the arc_id join N6 and the phase rows key on; U-HE-34 r6)
merge-gate-emit *ARGS:
    uv run python tools/merge_gate_log.py emit "$@"

# All three lens verdicts in ONE call (B-230 Task 5): concurrency, spec-conformance,
# witness-adequacy, always all three (a recorded BLOCK is a result, not an abort), exit
# the worst of the three (2 NOT recorded > 1 BLOCK > 0). No resumption -- repair one
# unrecorded lens with `merge-gate-emit` above, never by re-running this (that mints a
# fresh round for the two lenses that were fine).
#   just merge-gate-emit-all --pr <N> --arc-id <arc-id> --concurrency-json <f> --spec-json <f> --witness-json <f>
merge-gate-emit-all *ARGS:
    uv run python tools/merge_gate_log.py emit-all "$@"

# Absorption-step disposition write (C-HE-24 §5, U-HE-47): append ONE finding_adjudication
# row. Exit 0 recorded / 2 NOT recorded (unknown finding_id, actor == producer, same-second
# ts, an already-adjudicated lineage violation, or — with HARNESS_ARC_ID set — an arc this
# lane's reservation does not hold / a target row from another arc). Headless venues MUST
# use the prefixed form (the guard auto-allows only it, holder-bound):
#   HARNESS_ARC_ID=<arc-id> just merge-gate-adjudicate --finding-id <id> --disposition accepted|rejected --actor <runner>_absorber
merge-gate-adjudicate *ARGS:
    uv run python tools/merge_gate_log.py adjudicate "$@"

# Landing predicate: the head about to merge may differ from the head the lenses approved
# ONLY by the two gate-log files. Exit 1 (re-gate) otherwise.
merge-gate-landing-delta reviewed final='HEAD':
    uv run python tools/merge_gate_log.py landing-delta --reviewed {{reviewed}} --final {{final}}

# ─── MEMORY.md — byte-cap gate + idempotent index upsert ────────────────────
# NOT a semantic compactor (that stays the agent's call) — a deterministic
# byte-exact cap gate + idempotent upsert. See tools/memory_compact.py's header.
memory-compact *ARGS:
    uv run python tools/memory_compact.py "$@"

# ─── closure gate (R-IF-115) — the "harness coding fully closed" predicate ──
# Consolidates R-FS-1 + R-CL-Q1..Q4/D1/C1 must_pass into one report.
# Spec: .harness/audit/Closure_Gate_v1.md. Delegates to arc_ledger / overlay /
# substitution_ledger / roadmap — no new source of truth.
closure-gate:
    uv run python tools/closure_gate.py

# Exit 1 if any AUTOMATABLE Tier-1 predicate fails (manual ones reported only).
closure-gate-check:
    uv run python tools/closure_gate.py --check

# ─── R-CL-Q1 structured review artifact gate ────────────────────────────────
# Provider-free checker for the Q1 review/simplification evidence artifact.
q1-review-check report:
    @uv run python tools/q1_review_gate.py --check {{report}}

q1-review-schema:
    @uv run python tools/q1_review_gate.py --schema

# ─── R-CL-D1 documentation suite gate ──────────────────────────────────────
# Provider-free checker for the operator-facing docs suite and grounding matrix.
docs-completeness-check:
    uv run python tools/docs_completeness.py --check

# ─── U-MEM-25 memory substrate closeout gate ───────────────────────────────
# Provider-free checker for memory policy docs and closeout evidence.
memory-closeout-check:
    uv run python tools/memory_closeout_check.py --check

# ─── R-CL-C1 closure certification gate ────────────────────────────────────
# Provider-free checker for the final closure certificate and evidence links.
closure-certification-check:
    uv run python tools/closure_certification.py --check

# ─── operator-facing CLI smoke ─────────────────────────────────────────────

# One-shot run of a workflow manifest. Example: just run examples/minimal.toml
# Passes --config harness.toml (create it: cp harness.toml.example harness.toml).
run file:
    uv run harness run {{file}} --config harness.toml

# Materialize a temp harness config for a local external CLI provider.
# Examples:
#   just external-cli-config codex
#   just external-cli-config antigravity
#   just external-cli-config gemini       # legacy/deprecated Gemini CLI
#   just external-cli-config generic-command --provider-name local_llm --command my-llm --model demo --family openai --arg=--model --arg={model}
external-cli-config provider *args:
    @uv run python tools/external_cli_provider_config.py {{provider}} {{args}}

# Start the daemon (background MCP server over Unix socket).
daemon:
    uv run harness daemon --config harness.toml

# Client-side dispatch into a running daemon. Example: just run-daemon workflow.yaml
# (No --config: the running daemon already holds the loaded RuntimeConfig.)
run-daemon file:
    uv run harness run {{file}} --daemon

# ─── mechanism β — real-LLM e2e (env-gated on ANTHROPIC_API_KEY) ───────────
#
# These recipes hard-require ANTHROPIC_API_KEY. The justfile's dotenv-load
# pulls it from .env automatically; if missing, pytest skips the e2e tests
# with a clear cite and the recipe exits cleanly.

_require-anthropic:
    @if [ -z "${ANTHROPIC_API_KEY:-}" ]; then \
        echo "ERROR: ANTHROPIC_API_KEY not set."; \
        echo "  Add it to .env (copy from .env.example) or export it in your shell."; \
        exit 1; \
    fi

# All four mechanism-β e2e tests (AC #1, #3, #7, #8).
mech-beta: _require-anthropic
    uv run pytest harness-runtime/tests/integration/test_track_b_e2e.py \
        -k "real_anthropic or daemon_mode_equivalent or skill_activation or webhook_delivery" \
        -v

# AC #7: SkillActivationHook emits skill.* span under real LLM exercise.
# Advances H_T-AS-8d RETIRE-READY → RETIRED on green.
retire-as-8d: _require-anthropic
    uv run pytest harness-runtime/tests/integration/test_track_b_e2e.py::test_ac7_skill_activation_emits_skill_namespace_span -v

# AC #8: webhook_config delivery emits hitl.webhook.* span under real LLM exercise.
# Advances H_T-OD-5 RETIRE-READY → RETIRED on green.
retire-od-5: _require-anthropic
    uv run pytest harness-runtime/tests/integration/test_track_b_e2e.py::test_ac8_webhook_delivery_emits_hitl_webhook_span -v

# AC #1: single-step real-Anthropic inference round-trip.
mech-beta-ac1: _require-anthropic
    uv run pytest harness-runtime/tests/integration/test_track_b_e2e.py::test_ac1_real_anthropic_single_step_succeeds -v

# AC #3: daemon-mode equivalence to one-shot under real LLM.
mech-beta-ac3: _require-anthropic
    uv run pytest harness-runtime/tests/integration/test_track_b_e2e.py::test_ac3_daemon_mode_equivalent_to_one_shot_with_real_llm -v

# R-100 live e2e: real 3-step INFERENCE workflow against Anthropic (claude-haiku-4-5)
# through api.run (AC #1 + #3 + #4). Paid (~a few cents). live-green is the operator's run.
mvp-r100-real: _require-anthropic
    uv run pytest harness-runtime/tests/integration/test_r100_real_workflow_e2e.py -v

# Requires OPENAI_API_KEY (dotenv-loaded from .env), same shape as _require-anthropic.
_require-openai:
    @if [ -z "${OPENAI_API_KEY:-}" ]; then \
        echo "ERROR: OPENAI_API_KEY not set."; \
        echo "  Add it to .env (copy from .env.example) or export it in your shell."; \
        exit 1; \
    fi

# R-300 (B-2) live cross-family fallback: primary anthropic invalid-model -> real
# openai (gpt-4o-mini) through api.run. Proves the production RetryBreakerFallback
# dispatcher advances across provider families to a real second provider. Paid
# (~a few cents on openai; the 3 anthropic 404 attempts are unbilled). The
# deterministic counterpart (no creds) runs in CI; this is the live confirmation.
mvp-r300-cross-family: _require-anthropic _require-openai
    uv run pytest harness-runtime/tests/integration/test_r300_cross_family_fallback_e2e.py::test_r300_live_cross_family_fallback_against_real_providers -v

# R-300 (B-2) live OLLAMA exercise: same-family ollama fallback (invalid-model ->
# llama3.2:3b) through api.run, exercising the local-open-weight provider end to
# end. FREE — zero-token, zero-secret (local ollama daemon at 127.0.0.1:11434;
# `ollama list` auto-starts it). Skips cleanly if the daemon is unreachable. No
# key required (anthropic + openai degrade-optional).
mvp-r300-ollama:
    uv run pytest harness-runtime/tests/integration/test_r300_cross_family_fallback_e2e.py::test_r300_live_ollama_provider_fallback_exercise -v

# R-CL-P3 live multi-tier e2e: a full `api.run` workflow (echo-MCP TOOL_STEP +
# live-Ollama INFERENCE_STEP) exercised under each of the three bridging-arc
# persona tiers (SOLO / TEAM_BINDING / MULTI_TENANT). Proves the workflow
# completes against a real provider under every tier AND that config.persona_tier
# threads through the bootstrap to the bound §10.3 sampler base-rate (1.0/0.1/0.2),
# observed on the live run path. FREE — zero-token, zero-secret (local ollama
# daemon at 127.0.0.1:11434; llama3.2:3b). Skips cleanly if the daemon is
# unreachable. Closes capability-completion inventory item #8 (P3 live multi-tier).
mvp-r-cl-p3-multi-tier:
    uv run pytest harness-runtime/tests/integration/test_r_cl_p3_live_multi_tier_e2e.py -v

# R-CL-P3 redaction collector-boundary e2e: MULTI_TENANT pre-collector content
# redaction proven through the live R-420 collector → Tempo round-trip — a span
# carrying content sentinels is stripped by the production-materialized
# RedactionSpanProcessor before the BatchSpanProcessor exports, so content never
# reaches Tempo, while a structure attribute survives (selective redaction).
# DOCKER-gated — requires the R-420 stack up (just r420-self-hosted-stack-up); no
# provider inference, no secrets, no paid calls. Skips cleanly if the collector +
# Tempo ports are unreachable. Closes capability-completion inventory item #9
# (P3 redaction collector-boundary proof).
mvp-r-cl-p3-redaction-collector:
    uv run pytest harness-runtime/tests/integration/test_r_cl_p3_redaction_collector_live_e2e.py -v -m e2e

# ─── mechanism γ — multi-process orchestration (currently deferred) ────────
#
# AC #5 (SIGINT drain) + AC #6 (daemon-concurrent two clients) are marked
# @pytest.mark.skip pending per-session ctx isolation. Re-enable when that
# arc lands.

mech-gamma:
    @echo "Mechanism γ is currently deferred-with-cite (per-session ctx isolation)."
    @echo "See harness-runtime/tests/integration/test_track_b_e2e.py:373,385"
    uv run pytest harness-runtime/tests/integration/test_track_b_e2e.py \
        -k "sigint_mid_multi_step or daemon_concurrent" -v --no-header

# ─── housekeeping ──────────────────────────────────────────────────────────

# Show every test marker the suite uses.
markers:
    uv run pytest --markers

# Show currently-skipped tests with reasons.
skips:
    uv run pytest --collect-only -q -rs | grep -E "^SKIP" || echo "(no skips)"

# Check non-mutating host readiness for sandbox/cloud providers.
# Providers: r411-gvisor, r411-kata, r411-shuru, r411-microsandbox, r411-libkrun,
# r412-firecracker, r412-qemu-microvm, r421-e2b.
# Aliases: gvisor, kata, shuru, microsandbox, msb, libkrun, firecracker, qemu-microvm, microvm, e2b.
# Reviewed non-provider: mvm-sh/mvm is a Go bytecode VM, not an isolation sandbox.
sandbox-host-check provider='r411-gvisor':
    /usr/bin/python3 tools/sandbox_host_readiness.py --provider {{provider}}

# Live e2e for R-411 local TIER_3_MICROVM execution via Docker + gVisor/runsc.
# Uses only an already-local Docker image; set R411_GVISOR_DOCKER_COMMAND to
# target a non-default Docker host, e.g. a Lima VM rootful daemon.
r411-gvisor-live-e2e *args:
    uv run pytest harness-runtime/tests/integration/test_r411_gvisor_tool_execution_e2e.py -q {{args}}

# Live R-412 managed full-VM e2e. Requires E2B_API_KEY and creates one
# usage-billed E2B sandbox with outbound internet disabled.
# Codex must get explicit operator approval before running this command.
r412-e2b-full-vm-live-e2e *args:
    uv run --with e2b --package harness-runtime pytest harness-runtime/tests/integration/test_r412_e2b_full_vm_tool_execution_e2e.py -q {{args}}

# Check static readiness for the R-420/R-440 SELF_HOSTED_SERVER deployment gate.
# Non-mutating: does not start the daemon, contact OTLP, or fetch secrets.
self-hosted-readiness *args:
    uv run python tools/self_hosted_readiness.py {{args}}

# Build workspace wheels, export locked third-party requirements, and validate
# the R-CL-Q4 deploy image/readiness artifact surface.
q4-packaging-check:
    uv run python tools/q4_packaging_gate.py --build --check

# Start the local R-420 SELF_HOSTED_SERVER telemetry backend:
# OTel Collector Contrib + Tempo + Grafana. Requires Docker Desktop/daemon.
r420-self-hosted-stack-up:
    @bash -c 'source tools/hooks/lane-init.sh >/dev/null || exit 1; lane_stack_allowed; _rc=$?; [ "$_rc" -eq 0 ] || { [ "$_rc" -eq 3 ] && { echo "self-hosted stack NOT started: an uncleaned stack inherited from a reaped lane could not be removed (C-HE-11 5) — see stderr"; exit 1; }; echo "self-hosted stack skipped: RAM headroom below the bar for this lane (C-HE-11 5) — see stderr"; exit 0; }; _e="$(uv run python tools/lane_ports.py --shell)" || exit 1; eval "$_e"; docker compose -p "$R420_PROJECT" -f deploy/self-hosted-local/compose.yaml up -d'

# Stop and remove this lane's R-420 backend containers/network.
r420-self-hosted-stack-down:
    @bash -c 'source tools/hooks/lane-init.sh >/dev/null || exit 1; _e="$(uv run python tools/lane_ports.py --shell)" || exit 1; eval "$_e"; docker compose -p "$R420_PROJECT" -f deploy/self-hosted-local/compose.yaml down'

# Show this lane's R-420 backend container status.
r420-self-hosted-stack-status:
    @bash -c 'source tools/hooks/lane-init.sh >/dev/null || exit 1; _e="$(uv run python tools/lane_ports.py --shell)" || exit 1; eval "$_e"; docker compose -p "$R420_PROJECT" -f deploy/self-hosted-local/compose.yaml ps'

# Static R-420 readiness for a copied self-hosted config. Does not start Docker,
# the daemon, an OTLP probe, a secret fetch, or a provider call.
r420-self-hosted-readiness config:
    uv run python tools/self_hosted_readiness.py --config {{config}}

# Static R-421 MANAGED_CLOUD readiness for an operator-provisioned cloud config.
# Non-mutating: does not start the daemon, probe OTLP, fetch secrets, install
# SDKs, or call managed-cloud/E2B APIs.
r421-managed-cloud-readiness config *args:
    uv run python tools/managed_cloud_readiness.py --config {{config}} {{args}}

# Live R-421 hosted-sandbox candidate probe. Requires E2B_API_KEY and creates a
# usage-billed E2B sandbox. Codex must get explicit operator approval before
# running this command.
r421-e2b-live-probe *args:
    uv run --with e2b python tools/r421_e2b_live_probe.py {{args}}

# Live R-421 managed-cloud e2e. Requires GCP Secret Manager access, a real
# non-loopback managed OTLP endpoint, and creates a usage-billed E2B sandbox.
# Codex must get explicit operator approval before running this command.
r421-managed-cloud-live-e2e config *args:
    uv run --with e2b python tools/r421_managed_cloud_live_e2e.py {{config}} {{args}}

# Live R-420 local e2e. Requires Docker stack up, local Ollama, and keyring
# entries. No hosted-provider inference is performed by the default workflow.
r420-self-hosted-live-e2e config:
    uv run python tools/r420_self_hosted_live_e2e.py {{config}}

# Live R-430 collector proof. Requires the R-420 backend stack running locally.
# Emits no hosted-provider calls; only OTLP/Tempo localhost traffic.
r430-tail-keep-live-e2e config:
    uv run --package harness-runtime python tools/r430_tail_keep_collector_live_e2e.py {{config}}

# Live R-500 multi-tenant self-hosted proof. Requires the R-420 backend stack.
# Emits no hosted-provider calls; only OTLP/Tempo localhost traffic and a temp ledger.
r500-multitenant-live-e2e config:
    uv run --package harness-runtime python tools/r500_multitenant_selfhosted_live_e2e.py {{config}}

# Live R-830 S3 memory backend proof. Requires R830_S3_BUCKET plus ambient
# boto3-compatible AWS/S3 credentials. Performs real S3 create/view/update/delete
# against a unique object key and cleans it up.
r830-s3-live-e2e:
    uv run --with boto3 --with 'botocore[crt]' pytest harness-runtime/tests/integration/test_r830_memory_tool_s3_live_e2e.py -v

# Live B-36 / ADR-D8 proof of the AWS KMS SigningBackend (C-CP-20 §20.2.1).
# Requires B36_KMS_KEY_ARN, B36_KMS_REGION, B36_KMS_SIGNING_AWS_ACCESS,
# B36_KMS_SIGNING_AWS_SECRET (least-privilege identity scoped to one KMS key).
b36-kms-signing-live-e2e:
    uv run --package harness-cp pytest harness-cp/tests/integration/test_b36_kms_signing_live_e2e.py -v

# Live R-830 managed-DB memory backend proof. Requires
# R830_MANAGED_DB_CONNECTION_STRING for a PostgreSQL-compatible managed DB.
# Performs real create/view/update/delete against a unique /memories path and
# cleans it up.
r830-managed-db-live-e2e:
    uv run --with 'psycopg[binary]' pytest harness-runtime/tests/integration/test_r830_memory_tool_managed_db_live_e2e.py -v

# Live R-810 Files API proof. Requires ANTHROPIC_API_KEY plus a real
# non-loopback managed OTLP endpoint. Uploads, references, and deletes one
# Anthropic Files API file, then emits files.* telemetry through the managed
# collector. Codex must get explicit operator approval before running this command.
r810-files-live-e2e config *args:
    uv run python tools/r810_files_live_e2e.py {{config}} {{args}}

# Live R-820 Managed Agents proof. Requires ANTHROPIC_API_KEY plus a real
# non-loopback managed OTLP endpoint. Creates one short usage-billed Anthropic
# Managed Agents session and emits managed_agents.* telemetry through the
# managed-cloud collector. Codex must get explicit operator approval before
# running this command.
r820-managed-agents-live-e2e config *args:
    uv run python tools/r820_managed_agents_live_e2e.py {{config}} {{args}}

# ─── out-of-family review — Codex CLI (pilot) ──────────────────────────────
#
# Decorrelated second opinion alongside Claude's transcript-brief review
# (CLAUDE.md §13.1). That half = Claude reviewing Claude = correlated blind
# spots; Codex (OpenAI, out-of-family) gives
# DECORRELATED errors. The strongest signal is DISAGREEMENT between the two —
# surface that to the operator. See CLAUDE.md §10.9 (pre-merge adversarial gate).
#
# COST: runs on the operator's ChatGPT SUBSCRIPTION, not metered API. The guard
# below verifies "Logged in using ChatGPT" before any call; the flags force
# subscription auth even though dotenv-load injects OPENAI_API_KEY:
#   - `env -u OPENAI_API_KEY` hides the env key from codex
#   - `-c preferred_auth_method="chatgpt"` pins subscription auth
# If the OAuth login is ever absent/stale, the guard FAILS LOUD rather than
# letting codex silently fall back to a metered key. (Codex tool = H_E dev
# tooling, NOT H_T's OpenAI provider, which is metered-API per ADR-F1 / R-300.)

# Guard: refuse to run unless codex is logged in via the ChatGPT subscription.
_require-codex-subscription:
    @if ! command -v codex >/dev/null 2>&1; then \
        echo "ERROR: codex CLI not found on PATH."; exit 1; \
    fi
    @if ! env -u OPENAI_API_KEY codex login status -c preferred_auth_method="chatgpt" 2>&1 | grep -qi "ChatGPT"; then \
        echo "ERROR: codex is not logged in via ChatGPT subscription."; \
        echo "  Run 'codex login' (OAuth) to use the subscription, not metered API."; \
        echo "  (Refusing to run: a stale login could silently bill the API key.)"; \
        exit 1; \
    fi

# Reviewer model = gpt-5.6-sol at medium reasoning effort, pinned per invocation on every
# out-of-family review path (this recipe, tools/codex_review.py CODEX_OVERRIDES,
# tools/hooks/resolve_lib.sh) — operator direction 2026-09-15, token cost. Not inherited from
# ~/.codex/config.toml, whose top-level default is a different model. Probed 2026-09-15 on
# codex-cli 0.154.0 with no profile active: both `codex exec` and `codex review` honor
# `-c model=` and `-c model_reasoning_effort=`. The session rollout
# (~/.codex/sessions/**/rollout-*.jsonl) records the effective `"model"` and `"reasoning_effort"`.

# Out-of-family review of the committed branch HEAD vs BASE (default main), subscription auth.
# Routed through the fail-closed wrapper (C-HE-18): schema-parsed verdict, session-artifact
# fallback, REVIEWER_UNAVAILABLE on any parse failure. Exit 0 APPROVE / 1 BLOCK / 2 UNAVAILABLE.
# No `_require-codex-subscription` prerequisite: the wrapper strips OPENAI_API_KEY itself, pins
# `preferred_auth_method="chatgpt"`, and classifies a missing binary / stale login as
# REVIEWER_UNAVAILABLE(permanent) with a recorded row (C-HE-16 §4) -- the preflight would exit 1
# before that terminal contract could apply (codex round 8). Commit before reviewing: the
# verdict is bound to HEAD (C-HE-15 §3).
codex-review base='main':
    uv run python tools/codex_review.py --base {{base}}

# Out-of-family review of staged + unstaged + untracked changes, subscription auth.
codex-review-uncommitted: _require-codex-subscription
    env -u OPENAI_API_KEY codex review -c model="gpt-5.6-sol" -c model_reasoning_effort="medium" -c preferred_auth_method="chatgpt" --uncommitted

# Out-of-family diff review via Google Antigravity CLI (agy) — the decorrelated
# artifact reviewer when Codex is the AUTHOR (mirror of codex-review, which
# decorrelates Claude-authored work). Reviews the diff of the current branch vs BASE.
# Subscription path: agy serves the Google AI Ultra plan (Google-account login) —
# gemini-cli's consumer OAuth tiers were retired 2026-06-18 (IneligibleTierError);
# empirically verified 2026-08-01: `agy -p` headless works on the subscription.
# GEMINI_API_KEY/GOOGLE_API_KEY are stripped as insurance — justfile dotenv loads
# .env, which carries the harness runtime's own provider keys; those must never
# leak into review billing.
# Runs under the workspace interpreter (not /usr/bin/python3): since U-HE-06 the wrapper
# shares tools/review_wrapper_common.py (jsonschema) with codex-review -- schema-parsed final
# verdict, C-HE-16 classifier + bounded retry, C-HE-24 rows. Exit 0 APPROVE / 1 BLOCK / 2 UNAVAILABLE.
# `outcome_json` (optional): write the wrapper's own terminal envelope to that path -- the D-C
# failover (`just review-with-failover`) reads it instead of re-parsing raw vendor stdout.
# No `_require-antigravity` prerequisite (codex round 13): a missing `agy` is classified by the
# wrapper as REVIEWER_UNAVAILABLE(permanent) with a recorded row (C-HE-16 §4); the preflight
# exited 1 before that terminal contract could apply.
gemini-review base='main' outcome_json='':
    uv run python tools/agy_review.py --base {{base}} {{ if outcome_json != '' { '--outcome-json ' + quote(outcome_json) } else { '' } }}

_require-antigravity:
    @if ! command -v agy >/dev/null 2>&1; then \
        echo "ERROR: agy (Antigravity CLI) not found on PATH."; \
        echo "  Install per https://antigravity.google (Google AI Ultra subscription auth)."; \
        exit 1; \
    fi

# D-C failover chain (C-HE-17): codex-review, then gemini-review ONCE on REVIEWER_UNAVAILABLE,
# identical bar; the failover verdict blocks. Exit 0/1/2 as codex-review.
# NO `_require-codex-subscription` prerequisite here: a missing binary / stale login is exactly
# the permanent failure the wrapper classifies (C-HE-16 §4) and that MUST reach the failover
# (C-HE-17). The wrapper is the loud-failure surface: exit 2 + a `reviewer_unavailable` row --
# never a silent metered fallback (the wrapper strips OPENAI_API_KEY and pins chatgpt auth).
review-with-failover base='main':
    uv run python tools/codex_review.py --base {{base}} --failover

# U-HE-34 r3/r7: the logged variant for guarded/headless venues — the guard rejects
# `|` in Bash commands before its allowlist, so a session-level `... | tee round.log`
# can never run there; this recipe pipes the wrapper through the containment-owning
# publisher instead. ALL filesystem safety lives in tools/round_log_publish.py (one
# enforcer): an O_NOFOLLOW dir-fd walk from the repo root refuses a symlink at every
# component atomically — closing the parent-swap TOCTOU no pathname-based bash check
# can (codex r5-r7) — and its destination policy admits only relative paths under
# .harness/tmp/, so an auto-allowed invocation can never overwrite a tracked file or
# ledger. PIPESTATUS[0] preserves the wrapper's verdict exit (0 APPROVE / 1 BLOCK /
# 2 UNAVAILABLE / 3 GATE_REFUSED); a failed publish still shows the transcript, warns
# loud, and refuses to report a clean APPROVE over a missing/partial canonical log.
review-with-failover-logged log base='main':
    #!/usr/bin/env bash
    set -u
    # U-HE-50 (C-HE-27 §5 X6a): the wrapper's own process boundaries ARE the verify
    # edges -- start after admission (a refused launch spends nothing and opens no
    # span), end at the round's terminal. record_phase is first-write-wins and
    # replay-idempotent, so re-review rounds 2..n re-emit as no-ops and the durable
    # pair stays the round-1 window (ship-pr "Phase-span edges" stays the definition).
    # [LAW:single-enforcer] reservations.py record_phase is the only span writer;
    # [LAW:no-silent-failure] a failed emission warns loud but never alters the
    # verdict exit -- an absent span reads null downstream, never zero (C-HE-27 §3).
    # Partial-pair dispositions (codex u-he-50 r1): a lone END is refused here (a
    # failed start with a successful end would durably record a reversed pair), so
    # a start-failed attempt records nothing and the retry emits a fresh coherent
    # pair; a start-only OPEN window -- whether the attempt crashed before end or
    # its end WRITE failed (codex r2: only the next invocation can close it; no
    # durable same-attempt signal exists that would not mint a second authority) --
    # is CLOSED by the retry's end (U-HE-49: the retry keeps the round name), so
    # that recorded round-1 span is an upper bound that includes the interruption
    # and can overlap the edit window -- a named measurement bound, never a
    # gap-derived duration. Residual (codex r5, rejected-as-registered): a start
    # written before the wrapper's IN-PROCESS admit refuses (GATE_REFUSED) is
    # immutable and closes at the next real round -- that span includes the refused
    # attempt's gap; the session layer cannot see admit(), so the structural fix
    # (emission after admit, inside the process) is the B-218 wrapper-internal
    # emitter. Emission failure never aborts the round: a measurement write must
    # not gate the hard review path (absent span = legal null, C-HE-27 §3).
    _verify_start_failed=0
    emit_verify() {
      if [ -z "${HARNESS_ARC_ID:-}" ] && [ -z "${HARNESS_LANE_ID:-}" ]; then return 0; fi
      if [ -z "${HARNESS_ARC_ID:-}" ] || [ -z "${HARNESS_LANE_ID:-}" ]; then
        # codex r2 P3: half-set ids are a MISCONFIGURED invocation, not the spec'd
        # unreserved case -- losing the span silently would read as unreserved
        echo "review-with-failover-logged: WARN verify.$1 span not emitted -- HARNESS_ARC_ID and HARNESS_LANE_ID must both be set (half-set ids)" >&2
        return 0
      fi
      if [ "$1" = end ] && [ "${_verify_start_failed:-0}" = 1 ]; then
        echo "review-with-failover-logged: WARN verify.end skipped -- start emission failed this attempt; a lone end would record a reversed pair (C-HE-27 §3)" >&2
        return 0
      fi
      uv run python tools/reservations.py phase --arc-id "$HARNESS_ARC_ID" --phase verify --edge "$1" --lane-id "$HARNESS_LANE_ID" >/dev/null || {
        if [ "$1" = start ]; then _verify_start_failed=1; fi
        echo "review-with-failover-logged: WARN verify.$1 span emission failed -- round proceeds; span reads null (C-HE-27)" >&2
      }
    }
    # U-HE-49 (C-HE-21 §1 X6b): admission is evaluated BEFORE the launch -- a launch
    # the gate would refuse is not made (exit 3: no reviewer call, no log file, no
    # round identity consumed), and the verb mints a per-attempt destination
    # (r<N>-a<K>.log) so a refused/failed attempt never claims the write-once round
    # name its retry needs. Round identity keys on the r<N> prefix (arc_metrics
    # ROUND_ID_RE); the wrapper's own in-process admit() stays the enforcer of record.
    dest="$(uv run python tools/review_loop_gate.py launch --log "{{log}}" --base "{{base}}")" || exit "$?"
    if [ -z "$dest" ]; then
      echo "review-with-failover-logged: launch verb admitted but printed no destination -- aborting before the reviewer call" >&2
      exit 4
    fi
    emit_verify start
    uv run python tools/codex_review.py --base {{base}} --failover 2>&1 | uv run python tools/round_log_publish.py "$dest"
    rc=("${PIPESTATUS[@]}")
    if [ "${rc[0]}" = 3 ]; then
      # codex r4: the wrapper's own in-process admit (the enforcer of record) can
      # refuse AFTER the launch precheck admitted -- GATE_REFUSED is not a round
      # (C-HE-16 §3), so no end is recorded and a refused attempt can never land as
      # a complete verify pair in N6; a lone start closes at the next real round's
      # end (the named upper bound above).
      echo "review-with-failover-logged: NOTE verify.end not emitted -- GATE_REFUSED is not a round (C-HE-16 §3)" >&2
    else
      emit_verify end
    fi
    if [ "${rc[1]}" -ne 0 ]; then
      # Publish failure is its OWN terminal for EVERY reviewer outcome (codex r8):
      # exiting 1/2/3 here would let callers treat the round as valid while its
      # canonical log is missing, and round_metrics cannot detect an absent
      # intermediate round. The verdict itself is never lost -- the wrapper already
      # recorded its C-HE-24 rows and the transcript above carries the terminal line.
      echo "review-with-failover-logged: PUBLISH FAILED (exit ${rc[1]}) -- round log $dest missing/partial; wrapper verdict exit was ${rc[0]} (see transcript + gate rows)" >&2
      exit 4
    fi
    exit "${rc[0]}"

# B-215 admission-gate attest verbs (tools/review_loop_gate.py): deterministic
# entry/sweep attestations the wrapper enforces before any review round.
# `review-attest-budget` is deliberately NOT guard-allowlisted — extending the
# round budget stays operator-visible (the loop must never self-extend it).
# WR-10 (U-SR-04): labels before answers — the template verbs run preflight-grep.sh
# over the attested range FIRST and write every hit label (sweep: + outstanding
# finding ids) into a fresh answers template the author fills, so attestation
# passes on the first trial instead of by label discovery ([B] F14).
review-template-preflight answers base='main':
    uv run python tools/review_loop_gate.py template-preflight --answers {{answers}} --base {{base}}

review-template-sweep answers base='main':
    uv run python tools/review_loop_gate.py template-sweep --answers {{answers}} --base {{base}}

review-attest-preflight answers base='main':
    uv run python tools/review_loop_gate.py attest-preflight --answers {{answers}} --base {{base}}

review-attest-sweep answers base='main':
    uv run python tools/review_loop_gate.py attest-sweep --answers {{answers}} --base {{base}}

review-attest-budget extra reason:
    uv run python tools/review_loop_gate.py attest-budget --extra {{extra}} --reason {{quote(reason)}}

review-gate-check base='main':
    uv run python tools/review_loop_gate.py check --base {{base}}

# C-HE-22 reviewer-concurrency probe (U-HE-35): >=5 samples at each of N in {1,2,4}
# concurrent reviewer invocations on one fixed committed diff. GREEN iff median
# wall-clock at N <= 2x the N=1 median AND zero validity failures; RED => throttling
# assumed, pilots do not start (C-HE-13 §2). Live + provider-login-gated; samples
# land as C-HE-24 rows (producer=reviewer_concurrency_probe).
reviewer-concurrency-probe channel='codex' reps='5' base='main':
    uv run python tools/reviewer_concurrency_probe.py --channel {{channel}} --reps {{reps}} --base {{base}}

# U-HE-34 r4 NOTE: no session-layer result_capture recorder exists. The C-HE-27 §1
# process-exit vs log-write divergence is internal to the reviewer process — a
# synchronous in-recipe tee returns only after the log is flushed, so two session
# timestamps around it would measure one event. The split's recorder is registered
# as wrapper-internal work on B-218 (an earlier `review-log-settle` polling recipe
# was removed for exactly this reason).

# Advisory CodeRabbit review. This is optional and complements, not replaces,
# `just codex-review` and CI. Run after a meaningful diff exists.
_require-coderabbit:
    @if ! command -v coderabbit >/dev/null 2>&1; then \
        echo "ERROR: coderabbit CLI not found."; \
        echo "  Install/authenticate CodeRabbit before using this optional advisory gate."; \
        exit 1; \
    fi
    @if ! coderabbit auth status --agent >/dev/null 2>&1; then \
        echo "ERROR: coderabbit agent auth is not ready."; \
        echo "  Run: coderabbit auth login --agent"; \
        exit 1; \
    fi

coderabbit-review *ARGS: _require-coderabbit
    coderabbit review --agent "$@"

# Headless overnight autonomous runner (U-HK-15). Turns loop mode ON, then re-invokes
# `claude -p` in a BOUNDED loop until a genuine gate (halt marker) or the iteration cap.
# Approvals flow through the U-HK-12 permission guard (no --dangerously-skip-permissions).
# `just loop` runs for real; `just loop --dry-run` exercises the loop without calling claude;
# `just loop --max 10` caps iterations. Review the shared loop_status.md after a run.
# Custom multi-word prompt: the env var route still works and predates the "$@" fix:
#   HARNESS_LOOP_PROMPT="do X then Y" just loop
loop *ARGS:
    bash tools/04-loop/run.sh "$@"

# ─── U-HK-11 guardrailed loop-mode toggle (thin CLI dispatchers) ────────────
# Not to be confused with `just loop` (U-HK-15's separate overnight `claude -p`
# runner) or `just codex-loop-*` (the Codex autonomous-arc gate ledger). These two
# recipes run the EXACT commands the loop-start / loop-stop SKILL.md files
# document, giving a shell-invocable path to the same guardrailed auto-approve /
# Stop-continue tier (U-HK-12 / U-HK-14) without going through the Skill tool.

# Turn ON loop mode (see .claude/skills/loop-start/SKILL.md for the full contract).
loop-start:
    bash -c 'source tools/hooks/lib.sh && source tools/hooks/loop_lib.sh && \
        loop_activate "just loop-start" && \
        { loop_gc_worktrees reap || true; } && \
        loop_mode_active && echo "loop mode: ON"'

# Turn OFF loop mode (see .claude/skills/loop-stop/SKILL.md for the full contract).
loop-stop:
    bash -c 'source tools/hooks/lib.sh && source tools/hooks/loop_lib.sh && \
        loop_deactivate "just loop-stop" && \
        echo "loop mode: $(loop_mode_active && echo ON || echo OFF)"'
