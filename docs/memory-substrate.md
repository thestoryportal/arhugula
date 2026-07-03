# Memory Substrate

This page is the portable operator and maintainer guide for the memory
substrate. It summarizes the policy surface, runtime architecture, migration
posture, and C-MEM-20 verification matrix without running live provider or
external CLI calls. For a broader feature, workflow, and usage overview, start
with the [Memory layer README](memory-layer-readme.md).

## Operator Policy Guide

Memory access is explicit. A workflow that does not opt into a memory access
mode runs with no memory injection, no memory tool access, and no native
provider memory bridge. Provider-native memory, standard memory tools, prompt
packet fallback, and no-access mode are selected through typed access-mode and
CLI-profile carriers rather than ad hoc prompt text.

Captured memory starts as scoped, typed records. Episodic observations, semantic
records, preferences, procedural snapshots, compaction events, migration events,
redaction events, and operation-ledger rows all carry scope and provenance. The
promotion path is review-bound: model-authored observations cannot become
injectable memory until the promotion candidate is accepted under the memory
policy service.

Retrieval is policy-filtered before ranking. Scope, tenant, provider-family,
workflow, CLI profile, redaction state, tombstone state, and retention state are
checked before a record can contribute to a prompt packet, native adapter call,
or standard memory tool response.

Redaction and retention are fail-closed. A redacted or tombstoned record remains
auditable through ledger sidecars but is excluded from retrieval, context
assembly, and tool reads. Retention expiry and explicit tombstones are modeled
as memory operations, not as silent file deletion.

Live provider checks remain gated. Anthropic native memory behavior and
authenticated external CLI routes are represented in `LIVE_CREDENTIAL_GATES`;
the provider-free suite proves deterministic absence probes and fake-subprocess
routes, then names the operator surface needed to resume each live check.

## Maintainer Architecture Notes

The memory substrate is split by responsibility:

| Concern | Primary implementation |
| --- | --- |
| Record envelopes, paths, policy, retrieval, ledgers, redaction, observability | `harness-is/src` |
| Access-mode and tool-contract declarations | `harness-cp/src`, `harness-as/src` |
| Runtime context assembly, standard memory tools, native adapters, compaction, migration, durability | `harness-runtime/src` |
| Cross-provider verification matrix and live-gate declarations | [memory_verification_suite.py](../harness-runtime/src/harness_runtime/memory_verification_suite.py) |
| Closeout checklist gate | [memory_closeout_check.py](../tools/memory_closeout_check.py) |

The architectural rule is one source of truth per concept. Canonical records
live in the memory store; retrieval indexes are derived and can be rebuilt.
Operation ledgers record mutation intent and resulting state. Prompt packets,
provider-native calls, and tool outputs are projections of policy-filtered
records, never second authorities.

The C-MEM-20 matrix is declarative. It names deterministic pytest selectors for
schema validation, path traversal rejection, append-only ledger hash chains,
promotion review, poisoning resistance, compaction disposition, retrieval
determinism, cross-scope denial, prompt fallback, standard tools, native
adapter compatibility, CLI profile resolution, engine-class durability, and
redaction/tombstone exclusion. It also records live gates separately so local
and CI verification stay provider-free.

## Migration Notes

Migration is callback-backed and explicit. Existing storage backends can be
read through the storage-backend protocol selection surface, then reported as a
dry run without canonical writes. Applying a migration requires an explicit
native-memory `migrate` event and writes durable operation-ledger evidence.

Compatibility defaults do not silently promote old state. A migration report
names the source path, destination path, record category, status, and diagnostic
reason. The service can inspect older `/memories/...` layouts without mutating
canonical memory until the operator chooses the apply path.

Provider-native and external CLI migration checks are not automatic. Any resume
that needs `ANTHROPIC_API_KEY` or local CLI session auth must use the live-gate
surface named in `LIVE_CREDENTIAL_GATES`.

## Source Grounding

Portable grounding sources:

- Runtime memory modules under `harness-is/src`, `harness-cp/src`,
  `harness-as/src`, and `harness-runtime/src`.
- [memory-layer-readme.md](memory-layer-readme.md).
- [memory_verification_suite.py](../harness-runtime/src/harness_runtime/memory_verification_suite.py).
- [memory_closeout_check.py](../tools/memory_closeout_check.py).

The original harness governance closeout packet is not vendored into this
portable runtime package. This repo carries the runtime implementation,
operator summary, provider-free checker, and explicit live-gate declarations.
