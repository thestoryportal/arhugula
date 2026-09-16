# E8 — could dev-side Pixeltable serve as an evaluation oracle for the product's memory substrate?

*Design-phase note, 2026-09-16. Investigated against the cleared spec; nothing built. Posture: mode-agnostic; no `design-substrate/**` edit.*

## What the product's retrieval actually is

`design-substrate/Spec_Memory_Substrate_v1.md` C-MEM-11 (line 600) defines retrieval as a
request carrying `query_summary`, `scope`, `token_budget` and `allowed_kinds`, returning
`selected_refs`, `excluded_refs`, a `packet_hash` and a `ranking_trace`. The ranking factors
are listed explicitly: scope match, recency, confidence, source authority, explicit pinning,
failure-risk relevance, workflow and CLI-profile match, and the supersession, expiry,
redaction and denial filters. None of them is semantic similarity. The as-built index
(`harness-is/src/harness_is/memory_retrieval_index.py`, U-MEM-10) describes itself as "a
bounded metadata retrieval base" rebuilt from canonical records, with ranking and packet
assembly deferred to U-MEM-11.

C-MEM-20 (line 922) lists the required verification, and the retrieval item is "retrieval
determinism for fixed store/policy/request", alongside cross-scope denial and redaction
exclusion tests.

## Fit

A vector store is an oracle for a similarity ranker. The product's ranker is a rule ranker
over metadata with a deterministic trace. An embedding oracle would therefore test a
function the product does not implement; agreement or disagreement between the two would
say nothing about C-MEM-11 conformance. The determinism, denial and exclusion checks that
C-MEM-20 does require are property tests over fixed fixtures, which the repo already
expresses in pytest under `harness-is/tests/`.

The one defensible role is a versioned regression corpus: a table of (store snapshot,
request, `packet_hash`, `ranking_trace`) rows that a later ranker change is checked
against. Pixeltable's table versioning fits that shape, but a JSONL fixture under the test
tree does the same job with no server and no new dependency, and it stays inside the
process-isolation rule (X-AL-1) by construction.

## Verdict

Not a fit. Do not build. If the memory substrate ever adds a semantic ranking factor, that
is a design-phase change to C-MEM-11 first, and the oracle question re-opens then.
