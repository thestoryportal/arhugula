---
name: defect-class-preflight
description: Pre-commit self-review sweep against the recurring defect classes this workspace's reviewers actually catch, distilled from the merge-gate/codex finding corpus (the corpus only grows; scripts/refresh-classes.py rederives counts). Use BEFORE every commit of code in an arc — after writing or modifying any code under tools/ or harness-*/ and before invoking `just review-with-failover` or the merge-gate. Also use whenever about to claim a fix is complete, whenever a diff touches a shared surface (env variables, hooks, conftest, constants), whenever a review round's fix is being committed (fixes introduce their own defects), and whenever a diff introduces a new consumer of an existing data surface — another tool's log/ledger/store/output, an env variable, or an external SDK — which fires the new-consumer inventory pause at authoring time, BEFORE the consumer is written. One pass here is how a first draft survives review — skipping it is how arcs run 9–17 BLOCK rounds.
---

# Canonical Claude Workflow Bridge

Read `.claude/skills/defect-class-preflight/SKILL.md` completely from the workspace root before acting, including every reference it routes for the task. That canonical skill body is the full workflow; this Codex entrypoint does not summarize, trim, or replace it. Resolve its relative references from the canonical skill directory.

Apply these runner translations only:

- Root `AGENTS.md` is the Codex instruction entrypoint; targeted `CLAUDE.md` sections remain canonical lineage.
- Claude `Agent` or `Task` fan-out means fresh Codex subagents or `codex exec` contexts with self-contained briefs, subject to current delegation policy.
- `AskUserQuestion` means the available Codex user-input surface, and only for a genuine operator-owned fork.
- When Codex is the author, any canonical claim that Codex is out-of-family translates to Antigravity via `just gemini-review`; when Claude is the author, `just codex-review` remains out-of-family.
- Claude's transcript-brief-review judgment (a fresh-context Agent reviewer briefed on the session; CLAUDE.md §13.1) translates to an ISOLATED fresh-context reviewer in this venue — a separate `codex exec` call in the guard's isolated shape (`env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec --ephemeral --sandbox read-only -C <project> -c model="gpt-5.6-sol" -c model_reasoning_effort="medium"` with a constrained output path), handed the same written session brief, never the interactive controller reviewing its own work — plus the required out-of-family artifact review; where the venue's approval surface exposes no isolated exec shape (unattended loops), the discipline is not dischargeable unattended: the arc HALTS at the owing decision-fork/pre-done point (recorded — DEFERRED-HIL / handoff row) and resumes where an isolated reviewer is invocable (the Claude-venue transcript-brief Agent review, same written brief); a merge-gate lens is not a discharge (no brief; doc-only skips the gate) — never controller self-review, never a silent skip; never silently drop either function.
- Claude-only scratch paths translate to a safe `/tmp` or ignored workspace scratch path. All Git, CI, worktree, paid-call, secret, and destructive-action guardrails remain binding.
- The post-round adjudication step's `--actor <runner>_absorber` resolves to `codex_absorber` on this runner (U-HE-47: the disposition_actor records WHO adjudicated; a Codex absorption must never record Claude's identity).
