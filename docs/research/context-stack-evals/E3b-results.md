# E3b — drift scored only on claimed-identical pairs

Repo arhugula-v2; relocation 0f05512ca (parent f30eae667); split 50771f16c.

## Verdict (computed)

- A: claimed sections 33; not byte-identical or absent: **1**.
- C: lineage lines lost by the split: **0** (the source's own title, headings and preamble were replaced by the stub, by design).
- D: preserved-yet-re-tabled sections across the delta chain: **1**.
- B: stubs below both read thresholds: **0** of the pointer set; cosine vs containment rank agreement rho = 0.46, bottom-5 overlap 3/5.
- Drift, as defined (a claimed-identical pair that is not): **found, see A/C/D above**.
- What the embedding added over the normalised diff: nothing for A, C, D (identity questions). For B the two rankings DISAGREE (see rho/overlap); read both bottom-5 sets.

## A. Packs relocated byte-verbatim (the claimed § set, tested at the relocation)

| pack | claimed § | at relocation | since (lines −/+) |
|---|---|---|---|
| design-phase-principles.md | §10 | byte-identical | 0/0 |
| design-phase-principles.md | §10.1 | byte-identical | 0/0 |
| design-phase-principles.md | §10.2 | byte-identical | 0/0 |
| design-phase-principles.md | §10.3 | byte-identical | 0/0 |
| design-phase-principles.md | §10.4 | byte-identical | 0/0 |
| design-phase-principles.md | §10.5 | byte-identical | 0/0 |
| design-phase-principles.md | §10.6 | byte-identical | 0/0 |
| design-phase-principles.md | §10.7 | byte-identical | 0/0 |
| design-phase-principles.md | §10.8 | byte-identical | 0/0 |
| design-phase-principles.md | §10.9 | byte-identical | 1/1 |
| orchestration.md | §13.2 | byte-identical | 3/3 |
| orchestration.md | §13.3 | byte-identical | 1/1 |
| orchestration.md | §13.4 | byte-identical | 1/1 |
| orchestration.md | §13.5 | byte-identical | 2/2 |
| project-framing.md | §1.1 | byte-identical | 0/0 |
| project-framing.md | §7 | **CLAIMED BUT ABSENT from the pack** | — |
| project-framing.md | §9 | byte-identical | 0/0 |
| project-framing.md | §9.1 | byte-identical | 0/0 |
| roadmap-protocol.md | §12 | byte-identical | 0/0 |
| roadmap-protocol.md | §12.1 | byte-identical | 0/0 |
| roadmap-protocol.md | §12.2 | byte-identical | 0/0 |
| roadmap-protocol.md | §12.3 | byte-identical | 0/0 |
| roadmap-protocol.md | §12.5 | byte-identical | 1/1 |
| roadmap-protocol.md | §12.5.1 | byte-identical | 0/0 |
| roadmap-protocol.md | §12.5.2 | byte-identical | 1/1 |
| roadmap-protocol.md | §12.5.3 | byte-identical | 1/1 |
| roadmap-protocol.md | §12.5.4 | byte-identical | 0/0 |
| skills-and-subphases.md | §6 | byte-identical | 1/1 |
| skills-and-subphases.md | §7 | byte-identical | 0/0 |
| stack-and-layout.md | §3.3 | byte-identical | 0/0 |
| substitution-and-clearance.md | §4.1 | byte-identical | 0/0 |
| substitution-and-clearance.md | §4.2 | byte-identical | 0/0 |
| substitution-and-clearance.md | §4.5 | byte-identical | 0/0 |

## C. Artifact pointers split byte-preservingly by family

Pre-split file at 50771f16c^: 409,945 B, 73 paragraphs. Family files at 50771f16c: 8 files, 414,493 B, 123 paragraphs.
Paragraphs of the original missing from the union: **7**; paragraphs in the union not in the original: 49.
Lines of those missing paragraphs absent from the union at line level: **7**, of which lineage (not the source's own title/headings/preamble that the stub replaced): **0**. A re-cut table keeps its rows; a lost lineage row would be drift.

- MISSING paragraph: # CLAUDE.md §2 Artifact Pointer Lineage
- MISSING paragraph: This file is the relocated, byte-preserving reference body for root `CLAUDE.md` §2. It is process-substrate, loaded on demand when exact artifact lineage is nee
- MISSING paragraph: ## 2. Canonical artifact pointers
- MISSING paragraph: ### 2.3 Per-axis specs (Phase 5 canonical — contract authority)
- MISSING paragraph: | Axis | Spec | |---|---| | IS | `Spec_Information_Substrate_v1.md` (**v1.12 — canonical HEAD**, 2026-07-19 B-48 apply: C-IS-07 §7.1 timestamp-authority row + N
- MISSING paragraph: ### 2.4 Per-axis plans (Phase 6 canonical — execution authority)
- MISSING paragraph: | Axis | Plan | Unit count | |---|---|---| | core | `Implementation_Plan_Harness_Core_v1_3.md` (v1.3 — 2026-07-19 B-48 apply: NEW U-CORE-03 shared capacity erro
- LOST line: # CLAUDE.md §2 Artifact Pointer Lineage
- LOST line: ## 2. Canonical artifact pointers
- LOST line: ### 2.3 Per-axis specs (Phase 5 canonical — contract authority)
- LOST line: ### 2.4 Per-axis plans (Phase 6 canonical — execution authority)
- LOST line: It is process-substrate, loaded on demand when exact artifact lineage is needed.
- LOST line: Root `CLAUDE.md` keeps the operative pointer index so every session avoids loading this lineage by default.
- LOST line: This file is the relocated, byte-preserving reference body for root `CLAUDE.md` §2.
- EXTRA: # AS — artifact pointer lineage
- EXTRA: *Split byte-preservingly from `.harness/claude-artifact-pointers.md` at the U-CTX-03/04/05/06 R-CTX-1 context-optimization arc (2026-08-10, `B-17`/R-ICM-1 linea
- EXTRA: ## §2.3 spec pointer
- EXTRA: | Axis | Spec | |---|---| | AS | `Spec_Action_Surface_v1.md` (**v1.13 — canonical HEAD**, 2026-07-12 pointer catch-up per R-600 cadence-5 adjacent finding: this

## D. Project_Workflow deltas: sections preserved verbatim

- Project_Workflow_v1_10.md: claim source block; preserved §s named: 14; re-tabled headings: 2; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_11.md: claim source inline (8 sentences); preserved §s named: 4; re-tabled headings: 0; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_12.md: claim source block; preserved §s named: 8; re-tabled headings: 0; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_13.md: claim source block; preserved §s named: 13; re-tabled headings: 0; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_14.md: claim source block; preserved §s named: 20; re-tabled headings: 5; preserved-yet-re-tabled: **1** — §7.5
- Project_Workflow_v1_15.md: claim source block; preserved §s named: 13; re-tabled headings: 1; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_16.md: claim source block; preserved §s named: 13; re-tabled headings: 1; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_17.md: claim source block; preserved §s named: 13; re-tabled headings: 1; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_18.md: claim source block; preserved §s named: 13; re-tabled headings: 1; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_19.md: claim source block; preserved §s named: 13; re-tabled headings: 1; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_8.md: claim source inline (0 sentences); preserved §s named: 0; re-tabled headings: 86; preserved-yet-re-tabled: **0**
- Project_Workflow_v1_9.md: claim source block; preserved §s named: 43; re-tabled headings: 5; preserved-yet-re-tabled: **0**

A preserved § the same delta re-tables as a heading contradicts its own block. Re-tabled headings that are new sub-sections (the §1 amendment bodies) are not preserved §s and do not count.

## B. Root pointer stubs vs the pack section they name (30 pairs)

Lowest embedding cosine first; `contain` = share of the stub's content tokens present in the section.

| cosine | contain | pack | § | stub (truncated) |
|---|---|---|---|---|
| 0.74 | 0.75 | project-framing.md | §1.1 | The four design axes plus the CXA composition surface, and the per-axis scope table, are c |
| 0.77 | 0.17 | design-phase-principles.md | §10.1 | Which artifacts §10 governs, and what is out of scope for it. Body at `docs/governance/des |
| 0.78 | 0.57 | design-phase-principles.md | §10.3 | The live (uncommitted) design surface and its resolution paths. Body at `docs/governance/d |
| 0.79 | 0.93 | project-framing.md | §9.1 | This file is canonical for this workspace; revisions route to design-phase back-flow (§4.3 |
| 0.79 | 0.44 | stack-and-layout.md | §3.3 | The workspace tree (uv workspace root + `harness-{core,is,as,cp,od,cxa}/` members + `.clau |
| 0.79 | 0.75 | design-phase-principles.md | §10.6 | Deliverable mode vs conversational mode. Body at `docs/governance/design-phase-principles. |
| 0.79 | 0.75 | project-framing.md | §9 | Bootstrapped at Phase 6.5 Session 6 (ε), 2026-05-15. Detail at `docs/governance/project-fr |
| 0.80 | 0.90 | roadmap-protocol.md | §12.5.4 | Verify any memory or checkpoint claim empirically against HEAD before authoring against it |
| 0.82 | 0.71 | design-phase-principles.md | §10.4 | Source-grounding, confidence tagging, no-fabrication, byte-exact citation. Body at `docs/g |
| 0.82 | 0.83 | design-phase-principles.md | §10.5 | The five failure modes to actively prevent. Body at `docs/governance/design-phase-principl |
| 0.83 | 0.85 | orchestration.md | §13.5 | Council activation, out-of-family review, the overlay cite-grounding instrument, transcrip |
| 0.83 | 1.00 | orchestration.md | §13.2 | Solo (mechanical/linear) · transcript-brief review (decision-forks, transcript-aware) · `j |
| 0.83 | 0.94 | roadmap-protocol.md | §12.5.1 | Save patterns at cardinality ≥2; save feedback symmetrically; update rather than duplicate |
| 0.84 | 0.75 | skills-and-subphases.md | §7 | 7a bootstrap · 7b per-axis-stream implementation · 7c cross-axis composition · 7d substitu |
| 0.85 | 0.91 | design-phase-principles.md | §10.2 | The committed surfaces (persona, stack, deployment tiers, multi-LLM, ADRs, ADD, PRD, specs |
| 0.85 | 0.88 | design-phase-principles.md | §10.9 | Standing posture — council + adversarial reviewer + research corpus (2026-05-31). Body at  |
| 0.86 | 0.82 | roadmap-protocol.md | §12.3 | On drift: state `DRIFT DETECTED`, enumerate the divergence, present the three reconciliati |
| 0.86 | 0.75 | roadmap-protocol.md | §12.5.3 | After an R-NNN closes: memory check, memory refresh, checkpoint-resolved check. Body at `d |
| 0.87 | 0.71 | design-phase-principles.md | §10.8 | Multi-session continuity; surface contradictions before overwriting. Body at `docs/governa |
| 0.88 | 0.81 | orchestration.md | §13.3 | Normal/High is home base; the effort knob governs solo-pass thoroughness, not which §13.2  |
| 0.88 | 0.91 | roadmap-protocol.md | §12.1 | Fires automatically via the `SessionStart` hook (`tools/roadmap-audit/session-start.sh`).  |
| 0.89 | 0.83 | design-phase-principles.md | §10.7 | The eleven-voice council (Slate E11) at `.claude/skills/council/`. Body at `docs/governanc |
| 0.89 | 0.83 | roadmap-protocol.md | §12.5.2 | `/context-save-lean` (the workspace's trimmed copy of the gstack save flow, U-SR-08/WR-15) |
| 0.89 | 0.75 | roadmap-protocol.md | §12.5 | Three durable persistence mechanisms — roadmap+status (cross-session next action), auto-me |
| 0.89 | 0.97 | substitution-and-clearance.md | §4.2 | Per X-AL-2: **retirement = (cited unit IDs landed) ∧ (substituted H_E surface no longer in |
| 0.90 | 0.95 | orchestration.md | §13.4 | The 2026-06-01 resolver decision carried a nameable C10 ⊥ C11 tension and was council-elig |
| 0.91 | 0.88 | skills-and-subphases.md | §6 | Four Phase 7 skills ship at `.claude/skills/`: `phase-7-implementation`, `phase-7-cross-ax |
| 0.92 | 0.85 | roadmap-protocol.md | §12.2 | After any PR merges to main: recompute `workspace_state_hash`, update `.harness/roadmap_st |
| 0.92 | 0.91 | substitution-and-clearance.md | §4.5 | A design-substrate version accepted for Phase 7 consumption gets a clearance marker at `.h |
| 0.93 | 0.87 | substitution-and-clearance.md | §4.1 | H_E provides bounded substitutions for not-yet-built H_T primitives across 6 mechanism cat |

Rows with cosine < 0.60 AND contain < 0.50: 0. Rank agreement of the two scores: Spearman rho = 0.46; bottom-5 sets share 3 of 5 rows (cosine bottom-5: §1.1, §10.1, §10.3, §9.1, §3.3; containment bottom-5: §10.1, §3.3, §10.3, §10.4, §10.8).

