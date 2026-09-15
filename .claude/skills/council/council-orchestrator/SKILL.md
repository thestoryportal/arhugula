<!--
VENUE PROVENANCE — imported 2026-05-29 from Drive folder 1Je_dlorQQEIRp...
Originally authored for the Claude.ai design-phase project; now operates here
in this Claude Code workspace as part of the design-phase council. See workspace
CLAUDE.md §10 for design-phase operating principles. References to
`s2-orchestrator-design.md`, `s3-spec-writer-architecture.md`, `s4-c1-...`, etc.
are historical provenance pointers; the operative canonical for design-phase
work in this workspace is design-substrate/* (per CLAUDE.md §2).
-->

---
name: council-orchestrator
description: Multi-voice deliberation router for the agent harness council (Slate E11 — voices C1–C11). Use this skill when an agent-harness question's ownership spans more than one voice, when the right anchor voice is non-obvious, when multiple harness components are mentioned together (orchestration AND validation; observability AND reliability; tools AND safety), when the operator says "convene" or "what does the council think", uses the question-type vocabulary (architectural, contract, failure mode, tradeoff, cross-cutting), or names a cross-cutting concern (security, blast radius, observability, cost, reliability, eval-ability, HITL, local-first). Do NOT use when the operator names a specific voice (C1, C5, etc.) and asks a single-domain question, when the question is clearly single-domain technical (one tool's idempotency posture; a backoff curve; one span attribute), or for non-council tasks. Emits a Convening Block, CCR, primary/consultant voice contributions, and a TENSION block when voices disagree.
---

# Council Orchestrator

The Council Orchestrator routes a topic across the 11-voice agent harness council (Slate E11 — voices C1 through C11) and produces a structured multi-voice deliberation. It is a **deliberation orchestrator over prose**, not a runtime classifier over API traffic — voices are convened within one model call, not over separate inference requests.

This skill operates against the locked design captured at this skill's `references/` files (transitively rooted in the Drive-archived `s2-orchestrator-design.md` for provenance). Do not relitigate routing-architecture decisions during a session — those are settled. The skill's job at runtime is to *apply* the design to the operator's topic.

---

## Activation discipline

**Use this skill when:**

- The operator's prompt is a substantive design question about the agent harness and ownership is **not obviously** a single voice's exclusive domain.
- The prompt mentions multiple harness components together (orchestration AND validation; observability AND reliability; tools AND safety).
- The operator uses question-type vocabulary explicitly — "architectural," "contract," "failure mode," "tradeoff," "cross-cutting."
- The operator invokes the council explicitly — "what does the council think," "convene," "multi-voice perspective on…"
- The operator names a cross-cutting concern by name — security & blast radius, observability, cost, reliability, eval-ability, HITL/local-first.

**Do NOT use this skill when:**

- The operator names a specific voice (`C1`, `C5`, `C11`) — the named voice's individual skill triggers directly. The orchestrator does not wrap a single-voice consultation.
- The question is clearly single-domain — *"what's the idempotency posture for this tool?"* is C4 alone; *"what's the backoff curve here?"* is C9 alone; *"what span attributes does this event need?"* is C7 alone. Routing is unambiguous and no other voice has a stake.
- The prompt is a follow-up turn to a single-voice consultation. The active voice's skill carries the conversation; the orchestrator does not insert itself.
- The task is non-council (general coding help, file editing without council framing, document writing, debugging unrelated code).

If you're unsure whether a question warrants the orchestrator vs. a single voice, apply the **nameable-tension discriminator** (standing amendment 2026-05-31): can you name in advance a tension you expect to surface between two or more voices? If yes — convene. If no — route to single voice + a transcript-brief review (CLAUDE.md §13.1). The pilot at H_T-IS-2 (2026-05-31) demonstrated that the council's load-bearing value is tension-surfacing, not collegiality — convening voices that will all concur is the primary-collapse failure mode and wastes tokens. The Convening Block's *"voices considered, not convened"* field is the operator's debugging surface for this judgment.

**Default convening size is 2 (dyadic), not 3** (standing amendment 2026-05-31). Most genuinely multi-axis calls have exactly 2 substantive perspectives; the 3rd voice often concurs without rationale. Reserve 3-voice + 4-voice convening for tensions where you can name two distinct framings AND a third axis-specific concern. Hard cap at 5 unchanged.

---

## What this skill produces

Every response from this skill follows a fixed structure, in order:

1. **Convening Block** — voices convened, question type classification, routing rationale per voice, voices considered but not convened, pointer to CCR. Format: `references/output-templates.md`.
2. **CCR (Cross-Cutting Receipt)** — table addressing all six cross-cutting concerns (Touched / Owner status / Pre-check note). Format: `references/output-templates.md`.
3. **Voice contributions** — primary first, then consultants. Each consultant produces concur-with-rationale, surface-tension, or propose-refinement (no "no comment"). Format: see `references/output-templates.md`.
4. **TENSION block** — only if convened voices disagreed. Each TENSION entry: parties, issue, positions, stakes, status. Omit the block entirely if no tensions surfaced (do not write *"No tensions"*). Format: `references/output-templates.md`.

The structure is the contract surface. Downstream the spec-writer skill ingests these artifacts as structured embedded inputs — keep field names stable across responses.

---

## Workflow at runtime

When you trigger on an operator topic, work in this order:

### 1. Read inputs

- The operator's prompt (the topic).
- Workspace canonical artifacts relevant to routing — per workspace CLAUDE.md §2 (ADRs, ADD v1.3, PRD v1.1, per-axis specs, per-axis plans, CXA v2.16, Workflow v1.12). For routing summary: `references/voice-roster.md` (in this skill); for the four-layer signal procedure: `references/routing-rubric.md`.
- When a voice's substantive perspective is needed (during convening, in step 4 below), read the voice's individual SKILL.md at `.claude/skills/council/cN-{name}/SKILL.md` for its activation triggers, scope, tensions, and cross-cutting obligations.

### 2. Apply Layer A (operator override) — terminal if present

If the operator named voices explicitly (`"C1 + C5 on this"`) or tagged a question type explicitly (`"this is a tradeoff question"`) or invoked a full-council pass (`"full council"` — warn about context cost first), honor it and skip to step 5. See `references/routing-rubric.md` "Layer A" for full procedure including invalid-override handling.

### 3. Apply Layer B (question-type templating) and Layer C (scope-keyword scoring)

Classify the prompt into one of five canonical types — architectural, contract, failure-mode, tradeoff, cross-cutting. Then score each voice against the prompt using the voice's keyword profile. See `references/routing-rubric.md` "Layer B" and "Layer C" for full procedure.

Convene the top scorers up to default size (**2 voices — dyadic mode**, per 2026-05-31 standing amendment) or hard cap (5 voices), respecting voice asymmetry — exactly one primary, optional second co-primary, remainder consultants. Expand beyond 2 voices ONLY when (i) you can name a distinct third axis-specific concern AND (ii) Layer C scoring places a 3rd voice meaningfully above threshold. See `references/routing-rubric.md` "Convening size policy."

If classification is genuinely ambiguous (the prompt reads as two question types at once), surface the ambiguity to the operator before convening rather than picking silently. Asking is cheaper than wrong-routing.

### 4. Emit Convening Block + CCR (pre-check), then route to voices

Emit the Convening Block per `references/output-templates.md`. Then emit the CCR in **slim mode** (standing amendment 2026-05-31): enumerate ONLY the Touched concerns with their Owner status + Pre-check note; Not-Touched concerns may be collapsed into a single `n/a` line listing the unaddressed concerns by name. Pre-check note for each Touched concern stays one sentence with concrete framing. CCR ritualization (6 verbose rows every time) is the failure mode being corrected; slim mode preserves signal at lower token cost. Err toward Touched when borderline; false-positive Touched still cheap.

Then write the convened voices' contributions in turn:

- **Primary first** — produces the load-bearing position. Frames what consultants react to.
- **Co-primary if applicable** — speaks alongside primary, must engage primary directly. Maximum two co-primaries.
- **Consultants** — each produces one of: concur-with-rationale, surface-tension, propose-refinement. *"Looks good"* alone is rejected — re-prompt internally for substantive rationale before emitting.

For each voice's contribution, source the voice's perspective from the voice's individual SKILL.md plus the design-substrate canonical for that voice's axis **at the CURRENT VERSION recorded in workspace `CLAUDE.md` §2 at session-start** (standing amendment 2026-05-31 — pre-bind discipline). Workspace per-axis specs evolve frequently (e.g., CP spec was at v1.28 at this amendment's authoring; IS spec at v1.3); freelancing a voice's position from SKILL.md memory without grounding against the current spec version is the stale-citation failure mode. Each convened voice's first cite in their contribution MUST be from the current canonical spec.

Optional but encouraged: cite **external authority** from the research corpus at `research/` (Pattern Reference Catalog v1.0; cluster deep-dives 1-5; thought-leader inventory) when the voice's position needs grounding beyond intra-spec authority. See `references/research-citations.md` for voice → cluster pointer mapping.

Do not fabricate voice positions; if you cannot produce a substantive contribution from a voice's encoded expertise grounded against the current spec version, that voice should not have been convened — recuse it (Layer D recusal procedure in `references/routing-rubric.md`) and note in the Convening Block.

### 5. Surface tensions (Layer 1 default) — with mandatory probe-first discipline

If two or more convened voices disagree, **BEFORE emitting the TENSION block, run an empirical probe at primary source for the disputed claim** (standing amendment 2026-05-31 — probe-first discipline). The H_T-IS-2 cascade-scope pilot (2026-05-31) demonstrated that tensions are scoped by deliberation but resolved by empirical probe at canonical artifacts: T1 (cascade-grouping shape) resolved at IS spec v1.3 §5.2 amendment 2 cite, not at deliberation. Council surfaces; specs decide.

Probe shape: 1-5 minute targeted grep / Read at the most specific canonical artifact relevant to the dispute. Document the probe finding inline in the deliberation BEFORE the TENSION block. If the probe resolves the tension cleanly in favor of one voice, surface the tension as "**surfaced + probe-resolved**" with the probe finding as the resolution rationale — both positions still preserved verbatim, but the resolution is named.

If the probe does NOT resolve the tension (e.g., the canonical artifact is genuinely silent on the disputed point), then emit a Layer 1 surfaced-unresolved TENSION block per `references/output-templates.md`. Do not smooth the disagreement; preserve both positions verbatim from the voices' turns; state the stakes neutrally.

**Before emitting the TENSION block, check whether the disagreement engages a known Layer-3 permanent tension.** The locked Layer-3 list:

- **T-perm-1: C4 ↔ C10** — capability vs. gating. Tunable parameter `per_tool_gate_level × per_mcp_server_trust_tier`. Resolved at H_T via C-AS-10 §10.3 4-tier blast radius + CP §19.1.1 4-axis floor composition.
- **T-perm-2: C2 ↔ C3** — within-turn vs. across-turn (read/write seam between active context and durable state). Resolved at H_T via IS spec read/write boundaries.
- **T-perm-3: C1 ↔ C9** — control-flow vs. reliability. Tunable parameter `topology_fault_handling`. Resolved at H_T via CP §22 ResumptionKind taxonomy + `engine.replay_disposition`.

If the disagreement engages one of these, surface the tension's *Status* as `promoted to Layer 3 (permanent tension — see ledger as T-perm-N)`. Do not re-prompt the operator about whether to escalate; the promotion is already settled. The orchestrator's job is to label, not to re-litigate. **Important:** the H_T design has already resolved these permanent tensions at canonical artifacts; revisiting requires Class 1 fork → ADR back-flow per CLAUDE.md §4.3, not in-session re-litigation.

If the operator subsequently asks for resolution of a surfaced (Layer 1) tension, run Layer 2 escalation per `references/output-templates.md`.

### 5b. Optional out-of-family Codex decorator (U-HK-19)

After a tension is surfaced (or a single-voice recommendation is emitted), you MAY append a **Codex decorator** — a single out-of-family cross-check, **NOT a 12th deliberating voice**. The council is Claude-reviewing-Claude (correlated blind spots); `just codex-review` (gpt-5.6-sol at medium reasoning effort, pinned per invocation; ChatGPT subscription, $0, X-AL-1 H_E tooling) gives a **decorrelated** read of the same artifact/tension. It composes with the §13.1 division of labor: council + the transcript-brief reviewer are the in-family reviewers; Codex is the out-of-family one.

**When to append it (opt-in, encouraged on high-blast-radius design):** the convened decision touches a concrete artifact or diff (a spec amendment, a contract shape, an impl seam), the tension is high-stakes, loop mode is active, or the operator asks ("add the Codex read"). **Skip it** for a pure preference call, a settled-routing question, or when there is no artifact for Codex to read.

**How:** frame the convened tension + the candidate resolutions as a crisp prompt; run `just codex-review` against the relevant diff, or `resolve_codex "<tension + options + context>"` (from `tools/hooks/resolve_lib.sh`) when there is no diff yet. Then emit a compact **Codex decorator** block after the TENSION block:

```
**Codex decorator (out-of-family cross-check)** — `concur` | `flag-gap`
- Verdict: <concur with the surfaced resolution | flags a gap the voices missed>
- Note: <one-to-three sentences; verbatim-ish from Codex, not re-spun>
```

`concur` strengthens confidence; `flag-gap` is the high-signal case — surface it to the operator (it is the decorrelated catch the in-family reviewers structurally miss). The decorator never auto-overrides the council; it is evidence the operator weighs. Paid/secret/destructive forks are still the operator's per `[[feedback-background-agent-no-unilateral-paid-calls-or-secret-relocation]]` — Codex (subscription dev tooling) is not a paid harness call, but it does not authorize one.

### 6. Audit your own response before emitting

Before sending, check:

- **Convening Block field completeness** — all five fields present, no missing *"voices considered, not convened"* (state *"None"* if none).
- **CCR completeness** — all six concerns addressed; Touched concerns each have a pre-check note (not just *"yes"*).
- **Consultant substance** — every consultant produced concur-with-rationale, surface-tension, or propose-refinement; no *"no comment"*; no formulaic concur with no rationale.
- **TENSION block hygiene** — included if voices disagreed, omitted if voices agreed (do not write *"No tensions"*).
- **Single-voice anti-pattern** — if all consultants concurred without rationale, you may be in a primary-collapse failure mode. Re-prompt yourself for substantive consultant contributions; if there's genuinely nothing to add, reduce convening size next time on this topic class.

---

## Failure modes to actively prevent

These are the orchestrator's standing failure-mode mitigations. Treat them as live constraints on every response, not just theoretical risks.

- **Routing miss** — wrong voices convened, right voice silent. Mitigation: the *"voices considered, not convened"* field in the Convening Block makes routing transparent so the operator can correct via override on the next turn. Layer-D self-volunteer surfaces missing voices during convening.
- **Convening inflation** — sustained drift toward 5 voices on most topics. Mitigation: target average convening size ~3.5 voices across topics. **When a Layer C scoring run yields more than 3–4 candidates, prefer `handled-by-reference` (citation in the relevant voice's response) over adding a 4th or 5th convened voice.** This is the active relief valve for staying under the cap — most cross-cutting topics naturally pull 5+ voices and would inflate without it. The cap of 5 is a hard ceiling, not a default.
- **CCR ritualization** — concerns get one-sentence formulaic notes that lose signal. Mitigation: spot audit on operator request or every fifth session — pick one Touched concern and require artifact-level evidence for that concern's pre-check note rather than a one-sentence framing.
- **Primary collapse** — primary anchors, consultants produce formulaic concurrence, output reads as single-voice with cosmetic council framing. Mitigation: §"Audit your own response" check above. If three consultants in a row produce *"concur"* with no rationale, surface the pattern explicitly to the operator and ask whether the convening is degenerating to single-voice.
- **Tension hoarding** — tensions get surfaced and never resolved, accumulating across sessions. Mitigation: tensions older than three sessions either get explicit Layer 2 resolution treatment or get promoted to Layer 3 permanent.
- **Stale H_T canonical citation** — voice cites the H_T design at an outdated version. Mitigation: always cross-check the cited spec version against the version recorded in workspace CLAUDE.md §2 row at session-start. Per-axis spec versions evolve frequently (e.g., CP spec is at v1.26 as of 2026-05-29).

---

## Reference files

- `references/voice-roster.md` — per-voice routing summary: anchored question types, consulted question types, scope-keyword profile (strong-trigger band), cross-cutting concerns owned, likely co-primaries, negative keywords. Use this for Layer B / Layer C routing decisions.
- `references/routing-rubric.md` — full four-layer routing procedure (operator override, question-type templating, scope-keyword scoring, voluntary self-volunteer), convening size policy, voice asymmetry rules, consulted-by-reference handling.
- `references/output-templates.md` — exact format of Convening Block, CCR, TENSION block, plus output ordering rules and field discipline.

---

## Workspace canonical anchors

When voice perspectives need substantive grounding beyond the voice's own SKILL.md, the canonical references at workspace are:

- ADRs: `design-substrate/ADR-F1.md` through `ADR-F5.md` + `ADR-D1.md` through `ADR-D6.md`
- ADD: `design-substrate/Architectural_Design_Document_v1_3.md`
- PRD: `design-substrate/PRD_v1_1.md`
- Per-axis specs: `design-substrate/Spec_Information_Substrate_v1.md`, `Spec_Action_Surface_v1.md`, `Spec_Control_Plane_v1_26.md`, `Spec_Operational_Discipline_v1_27.md`, `Spec_Harness_Runtime_v1.md`
- Per-axis plans: `design-substrate/Implementation_Plan_{Information_Substrate_v2_3,Action_Surface_v1_4,Control_Plane_v2_29,Operational_Discipline_v2_26,Harness_Runtime_v2_32}.md`
- CXA: `design-substrate/Cross_Axis_Composition_Document_v2_16.md`
- Workflow: `design-substrate/Project_Workflow_v1_12.md`
- Phase 7 substrate: `design-substrate/Phase_7_Meta_Architecture_v1.md`, `Phase_7_Kickoff_Prompt.md`, `Target_Stack_Commitment_v1.md`

Versions evolve; cross-check against workspace `CLAUDE.md` §2 row at session-start.
