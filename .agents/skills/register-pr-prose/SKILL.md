---
name: register-pr-prose
description: Authoring discipline for every piece of durable prose in this workspace — PR bodies, register close_outs (.harness/forward-register.yaml, post-phase-8-forward-register.md), arc-ledger entries, checkpoint files, and any docstring or comment that states a checkable fact. Use WHENEVER writing or updating a PR body, a register row, a close_out narrative, a grounding-pass section, or prose containing counts, §-cites, line numbers, or claims about what a mechanism does. Prose findings are the second-largest defect class in this workspace (145 of 1,084 findings at first distillation, 2026-08-24) and cost full gate rounds; this skill is how the prose passes the spec-conformance lens on the first read.
---

# Canonical Claude Workflow Bridge

Read `.claude/skills/register-pr-prose/SKILL.md` completely from the workspace root before acting, including every reference it routes for the task. That canonical skill body is the full workflow; this Codex entrypoint does not summarize, trim, or replace it. Resolve its relative references from the canonical skill directory.

Apply these runner translations only:

- Root `AGENTS.md` is the Codex instruction entrypoint; targeted `CLAUDE.md` sections remain canonical lineage.
- Claude `Agent` or `Task` fan-out means fresh Codex subagents or `codex exec` contexts with self-contained briefs, subject to current delegation policy.
- `AskUserQuestion` means the available Codex user-input surface, and only for a genuine operator-owned fork.
- When Codex is the author, any canonical claim that Codex is out-of-family translates to Antigravity via `just gemini-review`; when Claude is the author, `just codex-review` remains out-of-family.
- Claude's transcript-brief-review judgment (a fresh-context Agent reviewer briefed on the session; CLAUDE.md §13.1) translates to an ISOLATED fresh-context reviewer in this venue — a separate `codex exec` call in the guard's isolated shape (`env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec --ephemeral --sandbox read-only -C <project> -c model="gpt-5.6-sol" -c model_reasoning_effort="medium"` with a constrained output path), handed the same written session brief, never the interactive controller reviewing its own work — plus the required out-of-family artifact review; where the venue's approval surface exposes no isolated exec shape (unattended loops), the discipline is not dischargeable unattended: the arc HALTS at the owing decision-fork/pre-done point (recorded — DEFERRED-HIL / handoff row) and resumes where an isolated reviewer is invocable (the Claude-venue transcript-brief Agent review, same written brief); a merge-gate lens is not a discharge (no brief; doc-only skips the gate) — never controller self-review, never a silent skip; never silently drop either function.
- Claude-only scratch paths translate to a safe `/tmp` or ignored workspace scratch path. All Git, CI, worktree, paid-call, secret, and destructive-action guardrails remain binding.
