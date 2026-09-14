---
name: merge-gate
description: Use after out-of-family review and PR CI are green for a substantive arhugula-v2 code or hook PR, immediately before merge.
---

# Merge Gate

Run the repository's three-lens pre-merge gate in fresh Codex contexts. This complements
Antigravity and CI; it replaces neither.

## Scope and preconditions

1. Confirm the PR number, branch, base, and final code HEAD from non-empty Git/GitHub output.
2. Confirm the authorship-dependent out-of-family reviewer approved. For Codex-authored work
   that is Antigravity through `just gemini-review`.
3. Confirm all required checks on the reviewed PR HEAD and current base `main` HEAD are
   terminal green. Confirm no stale prior CI branch remains unresolved; inspect its PR,
   worktree, and unique commits before any cleanup.
4. Inspect the closed changed-file set. Substantive runtime, test, hook, or tool logic uses
   all three lenses. A documentation-only or terminating roadmap refresh may take a logged
   `GATE SKIPPED-PROPORTIONAL`; do not pretend a skip is an approval.
5. If the diff changes `.codex/hooks.json`, `.codex/hooks/**`, `tools/hooks/**`, or the Codex
   hook adapter/witness, run `just codex-hook-runtime-witness` on the final code HEAD and
   require its provider-free report to prove all lifecycle events, both tool phase pairs,
   one session identity, three model requests, and both tool effects before launching lenses.

## Three fresh reviewers

Read `.codex/notes/merge-gate-lenses/README.md` and all three lens prompts completely:

- `lens1-concurrency.md`
- `lens2-spec-conformance.md`
- `lens3-test-witness.md`

Launch one fresh, ephemeral, lifecycle-isolated, read-only `codex exec` per lens, preferably in parallel. Each
gets only its lens prompt plus this self-contained tail:

```text
PR under review: #<N> on branch <branch>, base main. The head is in the binding
file named below -- read it there rather than being told it here.
Review the local merge-base diff and enough surrounding source to judge it. Do not edit.
Immediately before your final line, print ONE fenced ```json block with exactly these keys:
verdict (APPROVE|BLOCK), findings (array of {severity: P1|P2|P3, location, message}; empty on
APPROVE, non-empty on BLOCK) and the six binding values — head_sha, base_sha, diff_digest,
reviewer_identity, prompt_version, config_hash — copied VERBATIM from the JSON file at
<binding-file path printed by `just merge-gate-binding <lens id>`>. No other keys.
End with exactly `VERDICT: APPROVE` or `VERDICT: BLOCK: <one-sentence reason>` as the final
non-empty line (a bare `VERDICT: BLOCK` without its reason is not a verdict).
```

Use the actual arc worktree with `-C`, `--ephemeral`, `--sandbox read-only`, and a distinct
`--output-last-message /tmp/arhugula-pr-<N>-lens<1|2|3>-<40-char-head>.md`. Put `--`
before the quoted prompt so prompt text cannot be interpreted as an option and the autonomous
permission guard can validate options independently from reviewed text. The prompt must be
one single-quoted literal with no embedded single quote; newlines and shell-looking review
text inside that literal remain data:

```text
env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec --ephemeral --sandbox read-only -C <arc-worktree> \
  --output-last-message /tmp/arhugula-pr-<N>-lens<1|2|3>-<40-char-head>.md \
  -- '<short instruction to read the named lens file, plus the self-contained tail above>'
```

Validate each invocation separately: exit 0, output file exists and is non-empty, and its
final non-empty line is exactly one permitted verdict. Missing, malformed, truncated, or
ambiguous output is `BLOCK`.

## Prompt authoring — the Codex translation of the laws:prompt rule

The Claude carriers (`merge-gate`, `fan-out`, `council-orchestrator`) require subagent
prompts to be authored by a delegated `laws:prompt` agent. `laws:prompt` is a Claude-plugin
skill this runner cannot load, so the rule translates rather than transplants — but it is
NOT waived here, and the `agent-prompt-advisory` PreToolUse hook does not reach this path:
these lenses launch through `codex exec`, not an `Agent` tool call, so this text is the only
thing carrying the rule on the Codex side (codex u-sr-03 r1 P2).

The translation: the canonical templates are the lens files this procedure actually loads —
`.codex/notes/merge-gate-lenses/lens1-concurrency.md`, `lens2-spec-conformance.md`, and
`lens3-test-witness.md` — together with the self-contained tail above. Instantiating those
with this PR's literal values (PR number, branch, blast-radius list, binding-file path) is
the sanctioned path, and it is what a normal launch already does. Departing from them — a
re-worded specialty, an added lens, an extra instruction — is AUTHORING, and authoring goes
through a fresh Codex subagent whose brief is to write the prompt and return only the prompt,
never through a prompt composed inline while mid-implementation. Both round-3 lens
corruptions came from an orchestrator hand-assembling lens input mid-task.

**Base case, and where it stops being automatic.** Launching an authoring subagent is itself
an invocation, so without an exemption the rule would recurse forever. The base case is the
literal brief `Author the subagent prompt described below; return only the finished prompt.`
plus the task description, which needs no further delegation.

Be honest about the venue, though: this delegation is **not auto-allowed in loop mode**, and
no wording here changes that. `_safe_codex_exec_command` in `tools/hooks/permission-guard.sh`
admits exactly one `codex exec` shape — `env HARNESS_CODEX_REVIEW_ISOLATED=1`, one each of
`--ephemeral`, `--sandbox read-only`, `-C <project dir>`, and an `--output-last-message`
matching `/tmp/arhugula-pr-<N>-lens[123]-<40-hex>.md`. An authoring delegate is not a lens,
so it cannot satisfy that pattern without claiming a lens identity it does not have, and the
allowlist is deliberately NOT widened to make this sentence true (codex u-sr-03 r3 P2).

The consequence is the right one rather than a gap: instantiating the three canonical
templates is the normal path and needs no delegation at all, so ordinary gate runs are
unaffected. Authoring a genuinely new or re-worded lens is rare and judgment-bearing, and it
surfaces to the operator as an approval — which is the correct gate for changing what the
reviewers are asked, not an obstacle to route around.

Before launching, publish each lens's binding with
`just merge-gate-binding merge-gate-<concurrency|spec-conformance|witness-adequacy>`. It
writes the six values to a file and prints ONLY that path (U-SR-03, charter WR-09): name the
printed path in that lens's prompt and have the lens read the values from it, so no binding
value is transcribed into the prompt or into the verdict block — that is where both round-3
corruptions came from.

One copy of the head DOES remain on this runner, and it is stated rather than glossed (codex
u-sr-03 r9 P2): `--output-last-message` must match
`/tmp/arhugula-pr-<N>-lens[123]-<40-hex>.md`, so the 40-character head is typed into that CLI
argument. It cannot be removed without widening `_safe_codex_exec_command`, which this arc
declines to do. It is a different risk from the one WR-09 targets, and a benign one: it is not
a value the lens copies into its verdict, and getting it wrong fails LOUDLY and before any
review runs — the permission guard simply refuses the command shape — rather than producing a
verdict bound to the wrong tree. Require,
immediately before the
`VERDICT:` line, one fenced ```json block matching `tools/review_schemas/merge-gate.schema.json`
(`verdict`, `findings`, the six values verbatim). After each run, copy the output file into
the worktree (`.harness/tmp/merge-gate-lens-<id>.txt`, gitignored); once all three exist,
record them in ONE call (B-230 Task 5):
`HARNESS_LANE_ID=<lane-id> just merge-gate-emit-all --pr <N> --arc-id <arc-id> --concurrency-json .harness/tmp/merge-gate-lens-concurrency.txt --spec-json .harness/tmp/merge-gate-lens-spec-conformance.txt --witness-json .harness/tmp/merge-gate-lens-witness-adequacy.txt`
(`--arc-id` is the RESERVATION id, e.g. `u-he-34` — omitting it defaults the row's `arc_id`
to `pr-<N>`, which breaks the N6/phase joins AND the U-HE-47 unique-catch join against the
preceding codex rounds, whose rows carry the reservation arc id; `<lane-id>` is the
`.harness/.lane-id` content, without which every row records the `-nolane` fallback).
It records the three lenses in that order, ALWAYS all three (a recorded BLOCK is a result,
not an abort), and exits with the worst per-lens code: 0 = APPROVE recorded, 1 = BLOCK
recorded, 2 = NOT recorded (JSONL row first, structured markdown line second, C-HE-23 §2;
the final `VERDICT:` line must agree with the block, exact-line match). A lens that exited 2
does not count — treat it as `BLOCK`, re-run THAT lens, and record it alone with
`HARNESS_LANE_ID=<lane-id> just merge-gate-emit --pr <N> --arc-id <arc-id> --lens <id> --verdict-json .harness/tmp/merge-gate-lens-<id>.txt`
(never by re-running `emit-all`, which mints a fresh round for the two lenses that were fine;
there is no resumption — the JSONL is the only record).

## Outcome

- All three approve: the `emit-all` call (plus any per-lens repair) is the machine record —
  after a repair the outcome is the worst of the three RECORDED rows at the reviewed head,
  never the last exit code seen; additionally append the
  PR/date/branch/head/verdicts/outcome row to `.harness/merge-gate-log.md`
  (`just merge-gate-log-check` is the consistency reducer).
- Any block: reconcile it against current HEAD. If real and mechanical, fix it, add the
  appropriate witness, re-run Antigravity and local/CI gates, then re-run the blocking lens
  against the delta. A broad code change invalidates all three approvals.
- Absorption adjudication (C-HE-24 §5, U-HE-47): for each gate `finding` row absorbed
  (fix applied) or refuted, append its disposition —
  `HARNESS_ARC_ID=<arc-id> just merge-gate-adjudicate --finding-id <id> --disposition accepted|rejected --actor codex_absorber`
  (the prefix is REQUIRED for the guard's auto-allow and holder-bound to this lane's
  live reservation; actor must differ from the lens producer, write-time enforced).
  The `finding_id` is on the emitted JSONL row. Exit 2 = not recorded; re-run.
- Automatic fix/re-gate pauses at a recorded-decision checkpoint every ten rounds (period by
  operator decision, 2026-08-01; a checkpoint, not a cap, under C-HE-21 §1). An eleventh
  substantive disagreement is a genuine decision point; surface all verdicts together
  rather than looping or choosing silently, and continue only on a recorded decision.

Commit and push the gate-log row before merge, then wait for CI on that final PR HEAD to be
green. The log-only commit does not require re-running approved lenses, but any code, test,
contract, or lens-input change does — prove which with `just merge-gate-landing-delta
<reviewed-head>` (exit 0: reviewed..final touches only the two gate-log files, approvals
transfer; non-zero: re-gate). Merge only after the final-head CI check and only with
current merge authorization. Re-read the final PR head SHA immediately before merging and
pin the operation with `gh pr merge <PR#> --squash --match-head-commit <final-head-sha>` so
a concurrent push fails closed. Never bypass branch protection.
