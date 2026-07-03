# Memory Layer System README

This README explains the portable Arhugula memory layer in two ways:

- Non-technical: what the system does, what it protects, and how an operator
  should think about it.
- Technical: the modules, functions, data flow, access modes, tool calls, and
  verification commands that make the feature work.

The memory layer is default-off. A workflow that does not opt into memory policy
continues to run with no memory injection, no memory tool access, and no
provider-native memory bridge.

## Plain-English Overview

The memory layer lets Arhugula remember useful work context without turning the
model into an uncontrolled diary.

It can store things like:

- operator preferences
- project conventions
- decisions made during a run
- failure learnings
- research notes
- tool events
- compacted summaries of prior turns

Before any of that memory can influence a future model call, the system checks
policy, scope, redaction state, and access mode. In plain terms, the harness
asks: is this memory allowed for this run, this workflow, this provider, and
this operator policy?

The memory layer has three delivery paths:

| Path | Plain meaning | Typical use |
| --- | --- | --- |
| Native provider memory | Let a provider that has its own memory API use the canonical harness memory store through an adapter. | Anthropic native memory routes. |
| Standard memory tools | Give a tool-capable provider neutral tools such as `memory.search` and `memory.read`. | OpenAI-style tool-calling routes. |
| Prompt extension packet | Add a bounded, read-only memory section to the system prompt. | Providers without native memory or memory tools. |

If none of those paths is allowed, the run uses `no_memory_access`. That is a
valid result, not an error.

## What Changed For Operators

The portable runtime package now includes a memory substrate:

- Memory records have typed envelopes and stable content hashes.
- Memory roots and paths are declared instead of inferred from loose strings.
- Canonical records are the source of truth.
- Retrieval indexes are derived and rebuildable.
- Retrieval is policy-filtered before ranking.
- Prompt packets are bounded by token budget and carry source references.
- Standard memory tools are provider-neutral and policy-checked.
- Anthropic native memory calls use a canonical-store adapter.
- CLI profiles identify external CLI routes and their allowed instruction or
  memory sources.
- Redaction, tombstones, retention, promotion, and migration write durable
  memory operation evidence.
- Provider-free checks are wired into `just check-local`.

The system does not automatically read secrets, import external CLI memory, or
run paid provider checks. Live provider and authenticated CLI routes stay behind
explicit gates.

## What Changed For Developers

The feature adds a typed memory surface across the portable packages:

| Layer | Main modules | Responsibility |
| --- | --- | --- |
| IS | `harness_is.memory_record_envelope`, `memory_store`, `memory_retrieval_index`, `memory_retrieval`, `memory_policy`, `memory_redaction`, `memory_operation_ledger`, `cli_profile` | Canonical records, policy, paths, retrieval, indexes, redaction, CLI profile schema, ledgers. |
| CP | `harness_cp.memory_access_mode` | Provider memory access-mode selection and denial tracing. |
| AS | `harness_as.memory_tool_contracts` | Provider-neutral memory tool contracts and policy requirements. |
| Runtime | `harness_runtime.memory_context`, `memory_tool_executor`, `memory_capture`, `memory_promotion`, `memory_compaction_safety`, `memory_engine_durability`, `cli_profile_loading`, `lifecycle.native_memory_adapter` | Runtime context assembly, dispatch integration, tool execution, capture, promotion, compaction, migration, native adapter, verification matrix. |
| Tools/docs | `tools/memory_closeout_check.py`, `docs/memory-substrate.md`, this README | Provider-free closeout and operator documentation. |

## Core Concepts

### Memory Record

A memory record is a stored unit of information. It has:

- `memory_id`: stable identifier derived from memory tier, kind, and content hash.
- `tier`: working, episodic, semantic, procedural, or durable.
- `kind`: record family such as preference, decision, convention, tool event, or
  procedural snapshot.
- `scope`: project, workflow, provider family, CLI profile, tenant, and
  visibility.
- `content_hash`: SHA-256 over canonical JSON content.
- `redaction_state`: active, redacted, or tombstoned.
- `source_refs`: where the memory came from.

Canonical record types live in `harness_is.memory_record_envelope`.

### Memory Policy

`MemoryPolicyDocument` controls capture, promotion, retrieval, injection,
native memory, standard tools, review, retention, and redaction.

The default policy is `DEFAULT_DISABLED_MEMORY_POLICY`, which denies memory by
default. This is compatibility behavior.

Important policy decisions:

| Decision | Values | Meaning |
| --- | --- | --- |
| Capture | `deny`, `summarize_only`, `capture_full`, `capture_redacted` | Whether new run content may be stored. |
| Promotion | `discard`, `keep_episodic`, `propose_semantic`, `promote_semantic`, `propose_procedural`, `promote_procedural` | Whether observations can become reusable semantic or procedural memory. |
| Access | `deny`, `retrieval_only`, `prompt_packet`, `standard_tools`, `native_provider` | How memory may be retrieved or exposed. |
| Review | `automatic`, `operator_required`, `forbidden` | Whether a promotion or memory-changing action needs review. |
| Retention | `retain`, `expire`, `prune`, `tombstone` | How long memory remains available. |
| Redaction | `none`, `redact`, `tombstone` | How sensitive memory is removed from future use. |

### Memory Scope

`MemoryScope` prevents cross-project, cross-workflow, cross-tenant, and
cross-provider leakage. A record can be visible at private, workflow, project,
tenant, or public scope, but the policy resolver still decides whether the
requested run may use it.

### Derived Retrieval Index

The derived retrieval index is a rebuildable search projection. It is not the
source of truth.

`DerivedRetrievalIndexStore.rebuild()` reads canonical semantic and procedural
records, writes a current `derived-retrieval-index/v1` snapshot, and marks the
index fresh. Canonical writes append stale markers so retrieval can refuse stale
indexes when freshness is required.

### Memory Packet

A `MemoryPacket` is the bounded, source-linked set of memory sections selected
for a run. It includes:

- packet id and packet hash
- token budget
- access mode
- selected memory refs
- policy ref
- packet sections with text and token estimates

Prompt-extension mode renders this packet as a read-only system prompt section.
Standard-tool mode returns refs and packet section ids through tool calls.
Native-provider mode adapts the canonical store to the provider memory callback.

## End-To-End Workflow

The memory layer follows this flow:

```text
operator policy + workflow context
        |
        v
CLI profile resolution
        |
        v
memory access-mode selection
        |
        +--> no_memory_access
        |
        +--> native_provider_memory
        |        |
        |        v
        |   canonical native adapter
        |
        +--> standard_memory_tools
        |        |
        |        v
        |   provider-neutral memory tools
        |
        +--> prompt_extension_packet
                 |
                 v
          read-only prompt packet

canonical store <--> derived retrieval index <--> retriever/ranker
        |
        v
durable memory operation ledger
```

Step by step:

1. The workflow supplies a model binding, fallback chain, CLI profile, policy,
   token budget, and memory scope.
2. `resolve_cli_profile()` resolves the active CLI profile and any allowed
   instruction or external memory source declarations.
3. `select_memory_access_mode()` chooses native provider memory, standard tools,
   prompt packet, or no access. It records denial reasons when access is refused.
4. `RuntimeMemoryContextComposer.compose_run_start()` retrieves eligible memory,
   assembles a packet, emits telemetry, and appends an injection decision to the
   memory operation ledger.
5. `RuntimeLLMDispatcher` composes the memory context into provider dispatch:
   - Anthropic and OpenAI prompt-packet mode get read-only system memory.
   - OpenAI standard-tool mode runs a continuation loop for `memory.*` calls.
   - Native memory routes use the canonical adapter when that route is enabled.
6. Tool and native memory calls write durable operation events.
7. Promotion and redaction paths keep model-authored memory from becoming
   injectable until policy allows it.

## Access Modes

| Mode | Constant | What happens | Denial behavior |
| --- | --- | --- | --- |
| Native provider memory | `MemoryAccessMode.NATIVE_PROVIDER_MEMORY` | The provider-native memory API talks to the canonical store through an adapter. | Denied unless native provider access policy allows it and the provider supports it. |
| Standard memory tools | `MemoryAccessMode.STANDARD_MEMORY_TOOLS` | The model can call provider-neutral tools such as `memory.search`. | Denied unless standard tool policy allows it and the provider supports tools. |
| Prompt extension packet | `MemoryAccessMode.PROMPT_EXTENSION_PACKET` | A bounded read-only memory packet is inserted into the provider system prompt path. | Denied if policy denies injection or token budget is empty. |
| No memory access | `MemoryAccessMode.NO_MEMORY_ACCESS` | No memory is retrieved, injected, or exposed. | This is the default when policy or capabilities do not allow memory. |

Ledgerable denial reasons include:

- `policy_denied`
- `token_budget_empty`
- `external_cli_auth_unavailable`
- `external_cli_route_mismatch`
- `no_supported_mode`

## Standard Memory Tools

The provider-neutral tools are declared in `harness_as.memory_tool_contracts`
and executed by `StandardMemoryToolExecutor`.

| Tool | Purpose | Required arguments | Output highlights |
| --- | --- | --- | --- |
| `memory.search` | Search eligible memory records and return source-linked refs. | `query`, `scope_ref`, `policy_ref` | `results`, `memory_ref`, `packet_section_ref`, `packet_hash`, `score` |
| `memory.read` | Read one allowed memory record by stable ref. | `memory_ref`, `policy_ref` | `memory_ref`, `record_kind`, `content_hash`, `policy_ref` |
| `memory.write_note` | Write an episodic memory note under capture policy. | `note`, `scope_ref`, `policy_ref` | `memory_ref`, `operation_ref`, `policy_ref` |
| `memory.propose_promotion` | Submit a candidate for semantic or procedural promotion. | `memory_ref`, `target_kind`, `policy_ref` | `promotion_ref`, `operation_ref`, `review_required` |
| `memory.request_redaction` | Request deletion/redaction evidence for a memory ref. | `memory_ref`, `reason`, `policy_ref` | `redaction_request_ref`, `operation_ref`, `policy_ref` |

Read-like tools are read-only. Write-like tools require durable memory operation
events and policy permission.

## Native Provider Memory

`CanonicalNativeMemoryToolBackend` adapts the canonical memory store to the
Anthropic native memory callback shape. It supports:

- `view`
- `create`
- `str_replace`
- `insert`
- `delete`
- `migrate_from_callback`

The adapter validates that paths stay under `/memories/`, checks native memory
and capture/retrieval policy, uses per-path async locks, and writes durable
memory operation evidence.

Live Anthropic behavior is not part of provider-free local checks. It is listed
as `live-anthropic-native-memory` in `LIVE_CREDENTIAL_GATES`.

## CLI Profiles

CLI profiles describe external CLI provenance without moving credentials.

Built-in profile kinds:

- `generic`
- `claude_code`
- `codex`
- `antigravity`
- `gemini_legacy`
- `custom`

`resolve_cli_profile()` validates that a route matches the selected provider,
loads explicitly declared instruction sources under an instruction root, and
reports whether external memory sources are read-only, ledgered import, or
bidirectional sync.

Import policies:

| Policy | Read external memory | Import to canonical memory | Mutate external source |
| --- | --- | --- | --- |
| `deny` | no | no | no |
| `read_only` | yes | no | no |
| `ledgered_import` | yes | yes | no |
| `bidirectional_sync` | yes | yes | only when source allows mutation |

Authenticated external CLI routes are live gates, not provider-free defaults:

- `live-claude-code-cli-auth`
- `live-codex-cli-auth`
- `live-antigravity-cli-auth`
- `live-gemini-legacy-cli-auth`
- `live-generic-command-cli-auth`

## Operator Usage

### Verify The Memory Layer Is Present

Run the provider-free memory gate:

```bash
just memory-closeout-check
```

Expected shape:

```text
ready: yes
```

Run the full provider-free local gate:

```bash
just check-local
```

This syncs dependencies, runs lint, typecheck, the memory closeout checker, and
the non-e2e test suite with provider credentials unset.

### Rebuild A Portable Package And Confirm The Docs Ship

```bash
just portable-source /tmp/arhugula-harness-portable-check
```

The copied package should include:

- `docs/memory-substrate.md`
- `docs/memory-layer-readme.md`
- `tools/memory_closeout_check.py`
- `tools/test_memory_closeout_check.py`

### Read The Verification Matrix

Open:

```text
harness-runtime/src/harness_runtime/memory_verification_suite.py
```

The matrix declares deterministic provider-free selectors and explicit live
credential gates. It does not run paid provider calls.

## Developer Usage

The memory layer is currently exposed as runtime/library surfaces. There is no
single broad `harness.toml` switch in this package that enables memory for every
workflow. Integrations should build a policy, scope, store, index, retriever,
runtime memory context, and dispatcher wiring explicitly.

### Define Policy

```python
from harness_is.memory_policy import (
    AccessDecision,
    CaptureDecision,
    MemoryPolicyDocument,
    PromotionDecision,
    ReviewMode,
)

policy = MemoryPolicyDocument(
    policy_id="policy:example-memory",
    enabled=True,
    capture_decision=CaptureDecision.CAPTURE_FULL,
    promotion_decision=PromotionDecision.PROPOSE_SEMANTIC,
    retrieval_access=AccessDecision.RETRIEVAL_ONLY,
    injection_access=AccessDecision.PROMPT_PACKET,
    standard_tool_access=AccessDecision.STANDARD_TOOLS,
    review_mode=ReviewMode.OPERATOR_REQUIRED,
)
```

Use the narrowest access values that satisfy the workflow. Keep `enabled=False`
for workflows that should not use memory.

### Select Access Mode

`select_memory_access_mode()` needs a model binding, fallback chain, CLI
profile, policy, token budget, memory scope, and provider capabilities or
reflected provider capabilities. It returns:

- selected access mode
- selected provider and model
- fallback primary
- CLI profile ref
- optional external route ref
- denial reason
- decision trace

### Compose Runtime Memory Context

Use `RuntimeMemoryContextComposer.compose_run_start()` when a run begins. It:

1. selects access mode
2. retrieves eligible records
3. assembles a packet when memory is allowed
4. writes an injection decision operation
5. returns a `RuntimeMemoryContext`

Pass that context into `RuntimeLLMDispatcher` or
`materialize_llm_dispatcher_stage()`.

### Dispatch With Prompt Packets

When access mode is `PROMPT_EXTENSION_PACKET`, the dispatcher calls
`compose_system_prompt_with_memory_packet()` and injects a read-only memory
packet into the provider prompt path.

The OpenAI branch uses a leading `role: system` message. The Anthropic branch
uses the top-level `system=` argument. If a payload already owns a conflicting
system prompt, dispatch fails loudly instead of silently merging prompts.

### Dispatch With Standard Tools

When access mode is `STANDARD_MEMORY_TOOLS`, the OpenAI branch can continue
tool-call turns through `StandardMemoryToolExecutor`.

The model emits a `memory.*` call. The dispatcher validates the call, builds a
`MemoryToolExecutionRequest`, executes it under policy, appends a tool result
message, and calls the provider again. Non-memory tool names in this loop fail
as payload shape errors.

## Safety Model

The memory layer is built around fail-closed behavior:

- Default policy denies access.
- Scope checks happen before retrieval.
- Redacted and tombstoned records are not retrievable outside audit paths.
- Derived indexes are rebuildable and never authoritative.
- Standard memory tools require matching `scope_ref` and `policy_ref`.
- Write-like tools append durable operation evidence.
- Native memory paths are constrained to `/memories/`.
- Live provider checks are explicit gates.
- External CLI imports require declared profile policy.

## Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| Memory does not appear in a run | Policy is disabled or access mode selected `no_memory_access`. | Inspect `MemoryAccessModeSelection.decision_trace` and `denial_reason`. |
| Prompt packet is empty | Retrieval found no scoped, active, policy-allowed records. | Check `MemoryRetrievalResult.excluded_refs` and index freshness. |
| Retrieval index fails as stale | Canonical memory changed after the last rebuild. | Run `DerivedRetrievalIndexStore.rebuild()` in the integration path. |
| Standard memory tool call is denied | `standard_tool_access` is not `standard_tools`, or refs do not match scope/policy. | Check `MemoryToolExecutionDeniedError` text. |
| Native memory path is rejected | Path escaped `/memories/` or used unsupported shape. | Check `normalize_native_memory_path()` and adapter errors. |
| CLI profile fails to resolve | Route does not match selected provider, or required sources are missing. | Check `CliProfileResolutionError`. |
| Live Anthropic or CLI checks are skipped | Credentials or local CLI session auth are not confirmed. | Use the live gate ids in `LIVE_CREDENTIAL_GATES`. |

## Verification Commands

Use these commands before claiming the memory layer is ready in the portable
package:

```bash
just memory-closeout-check
uv run pytest tools/test_memory_closeout_check.py -q
just check-local
just q4-packaging-check
just portable-source /tmp/arhugula-harness-portable-check
```

Provider-free checks should pass without API keys. E2E live provider checks
remain explicit operator actions.

## Glossary

| Term | Meaning |
| --- | --- |
| Canonical memory store | Filesystem-backed source of truth for memory records and operation ledgers. |
| Derived retrieval index | Rebuildable metadata projection used for retrieval and ranking. |
| Memory packet | Bounded source-linked context selected for a run. |
| Prompt extension packet | Memory packet rendered as read-only system prompt content. |
| Standard memory tools | Provider-neutral tools with names like `memory.search` and `memory.read`. |
| Native provider memory | Provider-specific memory API backed by the canonical harness store. |
| CLI profile | Declared provenance and source policy for an external CLI route. |
| Promotion | Review-bound path from episodic observation to reusable semantic or procedural memory. |
| Tombstone | Durable marker that makes memory unavailable while preserving audit evidence. |

## Related Files

- [Memory substrate guide](memory-substrate.md)
- [Memory verification matrix](../harness-runtime/src/harness_runtime/memory_verification_suite.py)
- [Memory closeout checker](../tools/memory_closeout_check.py)
- [Runtime dispatcher integration](../harness-runtime/src/harness_runtime/lifecycle/llm_dispatch.py)
- [Standard memory tool executor](../harness-runtime/src/harness_runtime/memory_tool_executor.py)
- [Native memory adapter](../harness-runtime/src/harness_runtime/lifecycle/native_memory_adapter.py)
