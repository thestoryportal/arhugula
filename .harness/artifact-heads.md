<!--
GENERATED FILE — do not hand-edit.

Regenerate with:  uv run python tools/artifact_heads.py --write
CI gate:          uv run python tools/artifact_heads.py --check

R-CTX-1 / U-CTX-11. Derived from the `.harness/clearance/` marker corpus, which is the
version-binding record per root CLAUDE.md §4.5 — NOT from prose. Family is derived from each
marker's `artifact:` path (the corpus carries two marker-filename conventions and 11 of the
filenames disagree with their own artifact field, so filenames are not consulted). Versions
sort as integer tuples, so v1.9 < v1.10 < v1.116. Derivation is fail-closed: an unparseable
marker aborts the build rather than being skipped.
-->

# Canonical artifact heads

The head version of every artifact family with a clearance marker, derived from
`.harness/clearance/`. `Markers` is the number of markers filed for the family; `Head marker`
is the one that resolved the head.

## `design-substrate/` families

| Family | Head | Cleared | Artifact | Markers | Head marker |
|---|---|---|---|---|---|
| `adr-d2` | `v1.3` | 2026-07-15 | `design-substrate/ADR-D2.md` | 1 | `adr-d2-v1-3-cleared-2026-07-15.md` |
| `adr-d5` | `v1.6` | 2026-08-09 | `design-substrate/ADR-D5.md` | 2 | `ADR-D5-v1-6-cleared-2026-08-09.md` |
| `adr-d6` | `v1.3` | 2026-08-12 | `design-substrate/ADR-D6_v1_2.md` | 1 | `adr-d6-v1-3-cleared-2026-08-12.md` |
| `adr-d7-memory-substrate` | `v1 (Proposed, 2026-07-01)` | 2026-07-09 | `design-substrate/ADR-D7_memory_substrate.md` | 1 | `ADR-D7_memory_substrate-cleared-2026-07-09.md` |
| `adr-d8-audit-signing-backend` | `v1 (Accepted, 2026-07-16)` | 2026-07-16 | `design-substrate/ADR-D8_audit_signing_backend.md` | 1 | `ADR-D8_audit_signing_backend-cleared-2026-07-16.md` |
| `cross-axis-composition-document` | `v2.23` | 2026-07-30 | `design-substrate/Cross_Axis_Composition_Document_v2_23.md` | 8 | `cross-axis-composition-v2-23-cleared-2026-07-30.md` |
| `implementation-plan-action-surface` | `v1.6` | 2026-07-15 | `design-substrate/Implementation_Plan_Action_Surface_v1_6.md` | 2 | `implementation-plan-action-surface-v1-6-cleared-2026-07-15.md` |
| `implementation-plan-control-plane` | `v2.53` | 2026-08-13 | `design-substrate/Implementation_Plan_Control_Plane_v2_53.md` | 23 | `implementation-plan-control-plane-v2-53-cleared-2026-08-13.md` |
| `implementation-plan-harness-core` | `v1.3` | 2026-07-19 | `design-substrate/Implementation_Plan_Harness_Core_v1_3.md` | 1 | `implementation-plan-harness-core-v1-3-cleared-2026-07-19.md` |
| `implementation-plan-harness-runtime` | `v2.63` | 2026-08-13 | `design-substrate/Implementation_Plan_Harness_Runtime_v2_63.md` | 22 | `implementation-plan-harness-runtime-v2-63-cleared-2026-08-13.md` |
| `implementation-plan-information-substrate` | `v2.9` | 2026-08-07 | `design-substrate/Implementation_Plan_Information_Substrate_v2_9.md` | 5 | `implementation-plan-information-substrate-v2-9-cleared-2026-08-07.md` |
| `implementation-plan-memory-substrate` | `v1.3` | 2026-08-06 | `design-substrate/Implementation_Plan_Memory_Substrate_v1.md` | 4 | `implementation-plan-memory-substrate-v1-3-cleared-2026-08-06.md` |
| `implementation-plan-operational-discipline` | `v2.36` | 2026-08-16 | `design-substrate/Implementation_Plan_Operational_Discipline_v2_36.md` | 10 | `implementation-plan-operational-discipline-v2-36-cleared-2026-08-16.md` |
| `memory-substrate-design` | `v1 (Proposed design back-flow packet, 2026-07-01)` | 2026-07-09 | `design-substrate/Memory_Substrate_Design_v1.md` | 1 | `Memory_Substrate_Design_v1-cleared-2026-07-09.md` |
| `prd` | `v1.2 (Proposed, 2026-07-01)` | 2026-07-09 | `design-substrate/PRD_v1_2.md` | 1 | `PRD_v1_2-cleared-2026-07-09.md` |
| `project-workflow` | `v1.19` | 2026-07-24 | `design-substrate/Project_Workflow_v1_19.md` | 7 | `Project_Workflow-v1_19-cleared-2026-07-24.md` |
| `spec-action-surface` | `v1.14` | 2026-07-15 | `design-substrate/Spec_Action_Surface_v1.md` | 6 | `spec-action-surface-v1-14-cleared-2026-07-15.md` |
| `spec-control-plane` | `v1.119` | 2026-08-13 | `design-substrate/Spec_Control_Plane_v1_119.md` | 94 | `spec-control-plane-v1-119-cleared-2026-08-13.md` |
| `spec-harness-runtime` | `v1.121` | 2026-08-13 | `design-substrate/Spec_Harness_Runtime_v1.md` | 84 | `spec-harness-runtime-v1-121-cleared-2026-08-13.md` |
| `spec-information-substrate` | `v1.13` | 2026-08-07 | `design-substrate/Spec_Information_Substrate_v1.md` | 11 | `spec-information-substrate-v1-13-cleared-2026-08-07.md` |
| `spec-memory-substrate` | `v1.3` | 2026-08-06 | `design-substrate/Spec_Memory_Substrate_v1.md` | 4 | `spec-memory-substrate-v1-3-cleared-2026-08-06.md` |
| `spec-operational-discipline` | `v1.42` | 2026-08-16 | `design-substrate/Spec_Operational_Discipline_v1_42.md` | 16 | `spec-operational-discipline-v1-42-cleared-2026-08-16.md` |

## Non-`design-substrate/` families

Markers filed against an artifact outside `design-substrate/`. Enumerated rather than
dropped, so a marker can never leave the corpus unaccounted for.

| Family | Head | Cleared | Artifact | Markers | Head marker |
|---|---|---|---|---|---|
| `class-1-fork-provider-construction-allowlist-semantic` | `§10-amendment (prefer-OAuth default; supersedes E-prod-3's False default forward)` | 2026-07-09 | `.harness/class_1_fork_provider_construction_allowlist_semantic.md` | 1 | `provider-construction-allowlist-prefer-oauth-cleared-2026-07-09.md` |
| `implementation-plan-he-loop-lanes` | `v1.0 + rev 2026-09-15 (U-HE-42 execution correction, as-built — the C-HE-33 §4 outcome-measure line at :7473 and the C-HE-28 coverage row at :7838)` | 2026-09-15 | `.harness/plan/Implementation_Plan_HE_Loop_Lanes_v1.md` | 19 | `implementation-plan-he-loop-lanes-v1-u-he-42-c-he-33-reading-c-cleared-2026-09-15.md` |
| `spec-he-loop-lanes` | `v1.7` | 2026-09-02 | `.harness/spec/Spec_HE_Loop_Lanes_v1.md` | 8 | `spec-he-loop-lanes-v1.7-cleared-2026-09-02.md` |
