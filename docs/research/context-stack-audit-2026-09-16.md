# Context-management stack audit — arhugula-v2, 2026-09-16

*Audit of the proposed six-component stack (Pydantic · Pixeltable · Graft · Graphify · an "Integrated Context Compiler" · Obsidian + DuckDB) against this repo, on the new Apple Silicon machine. Every repo claim carries a `file:line`; every upstream claim carries a URL or is marked unverified. Developer-side (H_E) tooling only — nothing here touches the product's memory substrate (§6).*

## Recommendation

Revive Graft first, because it is already integrated here and is currently dead on this machine. Then pilot one narrow addition: a local Ollama-embedded semantic index over the prose corpus (`design-substrate/` and `.harness/`), which is the one retrieval surface this repo has no instrument for. Skip Graphify, skip building a custom "context compiler", keep Obsidian as a human-only surface if you want one at all, and adopt DuckDB as the SQL layer over the existing graphs when the first query is written. The reasons are below; the short version is that the code half of the stack already exists here as Graft plus a deterministic spec-to-code overlay, the doc half was tried in June under a different name and rejected in writing, and the hardware upgrade removes the one blocker (torch) without changing that calculus.

What the stack will and will not do for session efficiency: it does not reduce the session-start preload. That figure is governed by the closed R-CTX-1 program (measured 76,656 tokens post-slim, operator-ratified Floor B terminal, `.harness/r-ctx-1-u-ctx-21-measurement-2026-08-11.md:16-18`). What it can reduce is per-task retrieval cost: a Graft query returns `file:line` hits for a few hundred tokens instead of the thousands a file read costs, and a prose index would do the same for the 274k lines of markdown that today are searched only by `rg`.

## The machine, verified

| Item | Finding |
|---|---|
| CPU / RAM | `sysctl` reports **Apple M3 Max**, 48 GB (the ask said M3 Pro; the chip string says Max) |
| Python | 3.14.7 (Homebrew); uv 0.12.13; project venv has Pydantic 2.13.4 |
| torch on arm64 | Resolves cleanly: `torch==2.14.0`, `sentence-transformers==6.0.1` (uv dry-run, Python 3.12). The Intel-era blocker recorded in the other repo (`winning-defense/scripts/build_index.py:44-46`) no longer applies |
| Also resolvable | `duckdb==1.5.5`, `tree-sitter==0.26.0`, `tree-sitter-python==0.25.0` |
| Ollama | Serving on localhost with `nomic-embed-text`, `mxbai-embed-large`, `llama3.1:8b`, `llama3.2:3b`, `mistral:7b` |
| Pixeltable | 0.7.5 as a uv tool (arm64, no torch); `~/.pixeltable` is 444 MB, 422 MB of it the other repo's Postgres data |
| Graphify | `graphifyy` 0.9.57 as a uv tool (arm64, no torch); latest is 0.9.63 |
| Graft | `@nanonets/graft` **0.18.0** under a mise Node shim (`~/.local/share/mise/installs/node/24/bin/graft`); **not on PATH** in Claude Code sessions, so the MCP server fails with ENOENT and every graft hook exits silently |
| Obsidian | App installed; `obsidian` CLI symlinked at `/opt/homebrew/bin/obsidian` (official CLI, Obsidian 1.12+) |

## What this repo already has

The proposed stack assumes a blank slate. This repo is not one.

- **Graft is integrated** (PR #1386/#1388, 2026-08-16): four lifecycle hooks in `.claude/settings.json:119-127,172-180,202-210,228-241`, an MCP entry in `.mcp.json`, a project skill at `.claude/skills/graft/SKILL.md`, a reachability detector `tools/graft_reachability.py` that reads its wiring graph, and a repo-authored replacement for its post-edit hook (`.claude/helpers/graft-mark-dirty.cjs:1-14`). The graph is gitignored (`.gitignore:207`) and per-machine.
- **A deterministic spec-to-code overlay** (`tools/semantic_overlay/`, R-IF-112 RESOLVED): joins spec cites, CXA seams, substitutions and file structure with no LLM, rebuilt from HEAD on every call (`tools/semantic_overlay/README.md:3,70-77`). Exposed as `just overlay-query` and the `overlay-query` skill.
- **A prior knowledge-graph-plus-dashboard evaluation that was rejected.** Understand-Anything (LLM extraction pass, tree-sitter, dashboard) was driven live on 2026-06-04. Verdict, all three findings HIGH: the GUI is the wrong interface for agents, a static LLM snapshot goes stale every PR and "would manufacture drift, not detect it", and it models code-to-code when this repo's load-bearing relation is spec-to-code-to-seam (`.harness/spec-code-overlay/stages/01-exploration/output/understand-anything-and-spec-cite-traceability.md:11-15`). The plugin was uninstalled under R-CTX-1 (`.harness/r-ctx-1-implementation-plan-v1.md:33`).
- **A hook layer, a roadmap protocol, and a memory system** that together are the "AI orchestration loop" of component 6: session-start audit, per-prompt next-action injection, pre/post-compaction checkpointing, subagent validation, lint gates (inventory in `.claude/settings.json:1-193`).
- **No vector store, DuckDB, Pixeltable, Graphify or Obsidian footprint anywhere**, dev-side or product-side (grep over `*.py *.md *.toml *.json *.sh *.cjs *.yaml`; the only hits are design-research mentions in council skill docs).

## Component verdicts

*Ordered by what matters most here, not by the proposal's numbering.*

### Pydantic — already the stack; nothing to adopt *(component 1)*

Pydantic v2 is the committed type-contract layer (root `CLAUDE.md` §3.1). Any ingest schema for a new index should use it. This component of the proposal is satisfied by the status quo.

### Graft — keep, fix, and extend; this is the real win *(component 3A)*

Graft is the "code skeleton" the proposal describes, and it is the one component with measured value here. Timings on this machine, taken this session over 23,231 symbols and 70,049 call edges:

| Operation | Intel (recorded) | M3 (measured today) |
|---|---|---|
| `graft check` (staleness) | 45.7–45.9 s (`graft-mark-dirty.cjs:8`) | 10.6 s |
| `graft build` (incremental) | 5.6 s | 12.5 s (rebuilt 9 stale files) |
| `graft ask` (ranked query) | — | 0.64 s |
| `graft callers` | — | 0.11 s |

Two consequences. First, the stock post-edit hook still cannot fit its 8-second child budget at 10.6 s, so the repo's replacement shim stays. Second, the deep layer has never been built: "meaning tier 0% complete — 23231 of 23231 nodes pending". Graft 0.18 can run that pass against any OpenAI-compatible endpoint (`graft --help`: `--provider openai --base-url … --model …`), which means Ollama on localhost at zero cost. That deep layer (per-symbol summaries and concept nodes) is most of what the proposal wants from "semantic tissue" on the code side, from the tool already installed.

What is broken today: the binary resolves only through the mise shim, the hook helper's baked path points at a Node version that no longer exists (`graft-hooks.cjs:7` names 24.15.0; installed is 24.21.0), and its fallback via `npm root -g` reaches Homebrew's npm, which has no graft. So in every Claude Code session on this machine the MCP server fails to connect, the hooks no-op silently (the exact failure shape registered at `.harness/graft-integration-notes.md:65-77`), and the graph was nine files stale until this session rebuilt it. The notes already classify graft as a local operator prerequisite, not something the repo bootstraps (`:46-63`).

### Pixeltable — optional, and only for prose; not the write-path coordinator *(component 2)*

The proposal makes Pixeltable "the master database tracking incremental asset states" for code, documents and media. For this repo that role is held by git plus deterministic rebuilds from HEAD, and the repo's own doctrine is explicit that derived indexes are useful but never authoritative (product side at `design-substrate/ADR-D7_memory_substrate.md:57`; dev side in the UA verdict above). There is no media. Putting a Postgres child process (Pixeltable bundles `pixeltable-pgserver` with pgvector, https://pypi.org/project/pixeltable-pgserver/) in front of the code path adds a second source of truth for nothing Graft does not already do in 0.6 seconds.

Where Pixeltable does earn a place is the prose corpus. `design-substrate/` (14 MB) and `.harness/` (33 MB) hold 1,636 markdown files that agents today search only with `rg`, and the governance explicitly says "query, do not read wholesale" (`CONTEXT.md:44`). An incremental embedding index over that corpus is a genuine gap. Pixeltable can fill it without torch: `pixeltable.functions.ollama.embed` is documented (https://docs.pixeltable.com/sdk/latest/ollama.md), `document_splitter` chunks markdown by heading/sentence/token limit (https://docs.pixeltable.com/howto/cookbooks/text/doc-chunk-for-rag.md), and the embedding index updates per changed row (https://www.pixeltable.com/blog/pixeltable-incremental-embedding-indexes). The other repo's pipeline is a working template for exactly this shape: `winning-defense/scripts/build_index.py` (content-hash incremental sync, Ollama `nomic-embed-text`, chunk view with embedding index).

Two cautions from that template. Throughput there was 0.9 chunks/sec and a full rebuild took about 6.5 hours (`winning-defense/14_SYSTEM-SKILL/HANDOFF_context-pipeline_20260909.md:27-35`); the M3 will be faster but I have not measured by how much. And Pixeltable has no file-watch or directory-sync (confirmed absent from the CLI reference, https://docs.pixeltable.com/platform/cli), so the other repo needed launchd jobs and hand-written staleness scripts. Here the natural trigger is the existing PostToolUse and Stop hooks, but any hook edit is a governance event in this repo (single hook PR, Codex re-trust ceremony, `.harness/r-ctx-1-implementation-plan-v1.md:21,50`).

**Measured on this machine (2026-09-16, pilot run).** The shared tool install was upgraded in place to Pixeltable 0.7.7 with torch 2.14.0 and sentence-transformers 6.0.1 (the winning-defense catalog was backed up with its server stopped, re-opened under 0.7.7, and counts 936 documents and 27,691 chunks; its MCP server env was rebuilt to match). The pilot indexed the eight governance packs plus the fourteen spec and plan heads from root `CLAUDE.md` §2.3–§2.4, with a Pydantic record (`path`, `family`, `title`, `sha256`, `n_bytes`) as the ingest boundary and an isolated data directory:

| Step | Result |
|---|---|
| Corpus | 22 files, 2.6 MB, all with an H1 |
| Insert + chunk view (`heading,token_limit`, 500 tokens, 50 overlap) | 0.6 s + 2.2 s → **39,256 chunks** |
| Embedding index, Ollama `nomic-embed-text` (batched UDF, 768-dim) | 286 s → **137 chunks/s** |
| Embedding index, `BAAI/bge-small-en-v1.5` via sentence-transformers on MPS | 138 s → **285 chunks/s** |
| Query latency, top-3 similarity | 49–395 ms |
| Accuracy on 5 paraphrased cite-grounding questions | 5/5 in the right document; 4/5 at the right contract section (both indexes) |

Two notes from the run. Ollama raw embedding measured 44 chunks/s earlier in the session with 2,000-character chunks; the 137 figure here is on 500-token chunks, so the two are consistent. And the stock `pixeltable.functions.ollama.embed` cannot be used directly as an index embedding because it carries no static output dimension; a ten-line batched UDF declaring `Array[(768,), Float]` is required, and it must live in an importable module, which is why the other repo pins `PYTHONPATH`. A full pass over this repo's entire markdown corpus (8.7 M tokens) extrapolates to roughly 25 minutes on the torch path.

A simpler alternative for the same gap: sqlite-vec or a DuckDB array column fed by a 60-line script with the same Ollama embed call. Pixeltable's advantage is that the incremental bookkeeping is already written and already installed; the cost is a resident Postgres. Either is acceptable; Pixeltable is the faster path to a pilot.

### Graphify — skip for this repo *(component 3B)*

Graphify's code layer is tree-sitter AST, deterministic and free (https://github.com/Graphify-Labs/graphify), which duplicates Graft. Its document layer is the part the proposal wants, and it needs an LLM extraction pass over every document. The other repo measured that pass at about 8.5 minutes per changed file on local Ollama (`winning-defense/CLAUDE.md:266-268`) and 2.47 million input tokens for 225 files (`graphify-out/cost.json`). This repo has 1,636 markdown files, one of them 1.6 MB (`design-substrate/Spec_Harness_Runtime_v1.md`). Even at ten times the Intel speed that is days of extraction, and the result is a static snapshot that the June verdict already rejected on staleness grounds. The other repo also found that stock `graphify update` re-extracts the whole corpus and destroys curated labels, and wrote its own refresh script to work around it (`HANDOFF_context-pipeline_20260909.md:52-75`).

The specific "link a code block to a business rule in an architectural document" relation the proposal cites is the one relation this repo already resolves deterministically, through docstring cites and the overlay (`just overlay-query --contract C-XX-NN`). Graphify would approximate with an LLM what the overlay reads exactly.

**Measured on this machine (2026-09-16, after the operator questioned the Intel figure).** Raw inference with `llama3.1:8b` runs at 704 tokens/sec prefill and 58 tokens/sec decode. Graphify's own Ollama extraction over the eight `docs/governance/` files (48 KB, git-tracked scratch copy) took **785 s wall clock** for 14,277 tokens in and 6,398 out, roughly 98 s per file against the Intel 8.5 min: about five times faster, and the first attempt timed out and was retried, so the true floor is perhaps twice that. Extrapolated to this repo's 8.7 million markdown tokens, a full pass is in the range of 5–10 days of continuous local inference, decode-bound on the JSON it emits, not prefill-bound. Output quality at 8B is the larger problem: 31 nodes and 35 edges from 48 KB of dense governance text, nearly every edge labelled the generic `cites`, one document present twice under two ids, and several edges that do not correspond to anything in the source (a checkpoint "cites" the arc ledger; settings "cite" roadmap status). The larger-model rerun with `gemma3:27b` on the same eight files took 338 s (no timeout retry this time; 14,683 tokens in, 2,691 out) and produced **8 nodes and 0 edges**: one node per file and no relations at all, with five extracted items dropped as out of scope. One run each is thin evidence, but the direction is clear: the larger model did not rescue extraction quality on this corpus, and the graph it produced is unusable. Accuracy that saves turns is the right test; on this evidence the graph would add turns, because an agent would have to verify each edge against the text it was meant to replace, or in the 27B case has no edges to use.

**Frontier-model rerun (2026-09-16, operator question).** The same eight files through Graphify's `claude-cli` backend, which dispatches this Claude Code session's own subagents on the subscription, took 264 s and produced **90 nodes and 152 edges** (58,112 tokens in, 30,703 out; 136 edges marked EXTRACTED, 16 INFERRED). The nodes are real section headings and named artifacts; a sample of 22 edges read against the source were all true statements of the documents (sub-phase 7b routes to the back-flow and retirement skills, the §10.7 council references the four axes, the §13.2 matrix references the overlay tool), with some relation labels loose ("calls" for a sub-phase that invokes a skill). So the doc layer is usable at frontier quality, and the local-model results above were a model problem, not a tool problem.

**What a full pass would cost.** The corpus is 9.15 M tokens by a real tokenizer (4.84 M in `.harness`, 3.72 M in `design-substrate`). The Claude backend sent about four times the raw document tokens (prompting plus chunk retries) and emitted output at roughly half the input, so a whole-corpus pass through the subscription is on the order of 36 M input and 19 M output tokens, $0 metered but many hours of subagent time (264 s per eight files serially; concurrency helps). Through the metered API with a direct backend at 1–1.5× document tokens: Sonnet 5 at $2/$10 per million lands around $70–130 for the pass, Opus 5 at $5/$25 around $180–330. Per-PR incremental re-extraction of a few changed files is cents. Cost is not the barrier.

**What still is the barrier, for the doc layer.** (1) Staleness: the graph is a snapshot; this repo merges several PRs a day and the June verdict on Understand-Anything stands: a graph not regenerated on every merge manufactures drift. Graphify's incremental scan handles this for code (below) and for changed docs, but the other repo found that stock `graphify update` renamed every curated label and blew up the node count, and had to write its own refresh script. (2) Authority: the relations that decide correctness here (which contract a file implements, which seam it is an endpoint of) are already resolved exactly by the overlay from docstring cites; an LLM-extracted "implements" edge is a second, fallible map of the same fact. The right use is the relations the overlay cannot see, cross-document prose links, not a replacement for it.

**The 30-file pilot, Sonnet versus Haiku (2026-09-16, on the operator's subscription).** Corpus: the eight governance packs, root `CLAUDE.md`, `CONTEXT.md`, `AGENTS.md`, the sub-agent boundary spec, the four axis `CLAUDE.md` files, the graft and clearance notes, the overlay README, and eight skill bodies; 430 KB, about 108k tokens. Same corpus, same backend, model chosen by `GRAPHIFY_CLAUDE_CLI_MODEL`; the two runs overlapped in time, so wall clocks are under shared load.

| | Sonnet | Haiku |
|---|---|---|
| Wall clock | 480 s | 400 s |
| Tokens in / out | 323,402 / 70,406 | 491,024 / 45,805 |
| Graph | 33 nodes, 111 edges | 52 nodes, 67 edges |
| Files with a node | 30 of 30 | 21 of 30 |
| Node granularity | documents (a citation map: 102 of 111 edges are `references` between files) | concepts (Class 1 fork, X-AL-3 triad, sa-od sub-agent, merge-gate) with 30 relation types |
| 20-edge sample, checked against source | 20 true, low information | 20 true, high information |
| 10 fixed questions | 6 land on a relevant node; misses are questions whose concept was never extracted | 7 land; misses are files Haiku skipped |

Two conclusions, neither the one the question expected. First, Haiku produced the more useful graph: concept-level nodes and specific relations, at a fraction of the token cost, with accurate edges. Sonnet's run collapsed to file-level nodes and a `references` map, which is true but is what `rg` already gives. Second, and more important, both 30-file runs are far worse than the eight-file Opus-default run above (90 nodes, 152 edges, section-level nodes from 48 KB). The backend splits the corpus into two chunks of fifteen files, and with 45 KB files in a chunk the model summarizes at document level. Extraction quality here is governed by chunk size before it is governed by model. A usable pass would need Graphify driven per file or per small group, which its incremental scan does naturally for changed files but its initial build does not.

**Per-file extraction with Haiku, same 30 files (2026-09-16).** Each file extracted alone through the Claude backend (one `claude -p` session per file, sequential) and the thirty graphs merged by node id:

| | Chunked Haiku (2 chunks) | Per-file Haiku |
|---|---|---|
| Wall clock | 400 s | 5,581 s (93 min) |
| Tokens in / out | 491,024 / 45,805 | 2,702,589 / 677,062 |
| Graph | 52 nodes, 67 edges | 590 nodes, 693 edges (608 EXTRACTED, 85 INFERRED) |
| Files with a node | 21 of 30 | 29 of 30 |
| 20-edge sample | 20 true | 17 true and specific, 2 trivially true, 1 wrong (a §14 convention "applies_to" sub-phase 7a) |
| 10 fixed questions | 7 | 9, now landing on section-located nodes (§4.3, §11.5, ship-pr line 268) |

Per-file is the configuration that works: an eleven-fold gain in nodes, typed cross-document relations (`cleared_by`, `implements`, `applies_to`, `authorizes`) that a passage index does not express, and question hits that name the section. Its price is the backend: every per-file call is a fresh Claude Code session, so 108k tokens of documents cost 2.7 M tokens of subscription quota and 93 minutes. Extrapolated, the whole 9.15 M-token corpus would be roughly 230 M tokens and two days of wall clock through the subscription, which is not a sensible use of it; through a direct API backend at 1–1.5× document tokens the same per-file pass over this 30-file corpus is about 150k tokens, roughly $0.30 on Haiku 4.5 or $0.05 on `gpt-5.6-luna`, and the full corpus $20–30 on Haiku.

Revised acceptance verdict. The chunked runs did not answer anything the Pixeltable index could not; the per-file graph does add one thing, typed relations between documents. Whether that earns a place is a scope question, not a quality one: it is worth keeping for the governance and skill corpus (30–60 files, incremental re-extraction of changed files on the Claude backend is cents of quota), and not worth running over the 1.6 MB specs or the `.harness` ledgers, where the Pixeltable index and the overlay already cover the questions asked. The earlier finding stands on the chunked configurations: every question the Haiku graph answered, the Pixeltable index answered at the section level with the passage attached, and the two it answered that Pixeltable would also answer (sub-agent routing, clearance markers) are relations a heading search finds. Verdict: not adopted for this repo on this evidence; the one configuration worth a further try is per-file extraction with Haiku on the governance and skill corpus, if a specific cross-document question ever arises that the index cannot answer.

**A Codex-side backend (operator question).** Graphify has no Codex backend; its custom-provider hook accepts only an OpenAI-compatible base URL, and there is no supported way to expose a ChatGPT subscription or the Codex CLI as such an endpoint (the bridges that exist are unofficial reverse-engineered proxies, and OpenAI's subscription terms treat bulk automation as per-token work: https://manifest.build/blog/banned-from-chatgpt-subscriptions/). So a Codex-family route means the metered OpenAI API. Current pricing (https://developers.openai.com/api/docs/pricing, fetched 2026-09-16): the cheapest current-generation model is `gpt-5.6-luna` at $0.20 input / $1.20 output per million, $0.02 cached input, with Batch at about half; `gpt-5.6-terra` is $2 / $12. A whole-corpus pass at the measured 1–1.5× document tokens and 0.15–0.45 output ratio is roughly $3–8 on luna standard, $2–4 on Batch. That is cheaper than any Claude route by an order of magnitude, but it is an unverified model on this task: no credible extraction-quality comparison against Haiku 4.5 was found, so it would need its own 30-file pilot before adoption. Cost was never the barrier for Graphify here; extraction granularity and staleness are, and a cheaper model changes neither.

### Graphify versus Graft on the code layer *(operator question: can Graphify replace Graft?)*

Measured head to head on this repo's 1,121 Python files, both with no LLM:

| | Graft 0.18 (wiring graph) | Graphify 0.9.57 (code layer) |
|---|---|---|
| Full build | 23,231 nodes, 70,049 edges | 36,346 nodes, 101,744 edges, 27.6 s |
| Edge types | calls, contains, imports, extends | calls (30,492), uses, contains, rationale_for (docstring links), references, imports, inherits, indirect_call, re_exports |
| Incremental after one edit | 12.5 s | 10.9 s |
| Callers question | `graft callers X`: exact inbound edges, 0.11 s | `graphify query`: BFS depth 2 over a 2,000-token budget, truncated 49 of 332 nodes |
| Blast radius of a diff | `graft blast` (built for CI) | none |
| File API at a glance | `graft skeleton` | none |
| Agent surfaces | MCP (6 tools), Claude Code hooks, per-file cards | MCP mode, watch mode, Obsidian export |
| Already wired here | hooks, MCP, skill, reachability tool, merge-gate pre-flight | nothing |

The research claim that Graphify "covers multiple context layers" is true: its code layer is at parity with Graft on structure and speed, and it adds docstring-derived edges and, with a frontier backend, a document layer Graft does not have at all. What it does not cover is the query shape coding agents use most. Graft answers "who calls this, exactly" and "what does this diff break" as precomputed edges in a tenth of a second; Graphify answers with a budgeted graph walk that truncates on a repo this size. Those are the questions the repo's merge-gate and reachability tool are built on.

Replacing Graft would therefore trade exact callers and blast radius for a broader but fuzzier graph, and would discard a working integration. Running both is cheap: Graphify's code build is 28 s and its incremental scan is 11 s. The sensible shape is Graft for exact code navigation, Graphify's frontier-backed doc layer for cross-document relations if a pilot on 30 files shows it answers questions the overlay and the Pixeltable index do not.

### "Integrated Context Compiler" — does not exist as a product; do not build one *(component 4)*

No tool ships under that name (searched; closest existing projects are CodeGraphContext, contextplus, code-graph-rag, GitNexus, Understand-Anything and Aider's repo-map). Here it would be custom code. This repo already has the bridge it needs: the overlay joins the semantic layer to files and has an `enrich-ua` mode that left-joins onto a code graph (`tools/semantic_overlay/README.md:40-55`), and `tools/graft_reachability.py` already consumes Graft's `wiring.json`. If a unified export is ever wanted, it is a small extension of those two, not a new subsystem.

### Obsidian — human surface only, if at all *(component 5A)*

The other repo settled this correctly: "Agents read Pixeltable and the graph; Robert reads Obsidian" (`HANDOFF_context-pipeline_20260909.md:122-123`). The proposal has agents "read structured metadata rules directly from the Obsidian vault". In this repo the rules live in `CLAUDE.md`, `CONTEXT.md` and the skills, with over 700 tracked `CLAUDE.md §` cite lines depending on them (`git grep -c`, this session); a vault copy would be a second authority that drifts. Pointing Obsidian at the repo as a vault costs nothing and gives you graph view and backlinks over the existing markdown for reading. Writing generated notes into it is the Graphify export, already declined. The official CLI (Obsidian 1.12, https://obsidian.md/cli) makes vault automation possible, but nothing here needs it.

### DuckDB — cheap query engine over the graphs; not the vector store *(component 5B)*

There is no DuckDB anywhere in the repo. Measured today over graft's `wiring.json` (27.8 MB, 23,231 nodes, 70,049 edges) with `duckdb==1.5.5` and no server: load 0.7–1.8 s, edges-by-relation 2 ms, top fan-in join 6 ms, "production symbols whose only callers are tests" 35 ms (339 rows; the repo's `tools/graft_reachability.py` takes 2.8 s and adds an AST rescue pass DuckDB does not), two-hop blast radius 11 ms. That is a real query layer over the graph, `overlay.json` and the YAML ledgers, from a single wheel. The verdict moves up from "defer" to "adopt as the analytical query engine when the first such query is written"; the reservation is only about its role as a vector store. The `vss` extension's on-disk HNSW persistence is still marked experimental with a data-loss caveat (https://duckdb.org/docs/current/core_extensions/vss.html), so it should not be the vector store. Adopt it the first time someone writes the query that needs it.

### Orchestration loop — exists; the gaps are operational *(component 6)*

The hooks, roadmap protocol and memory system already are this component. The three concrete gaps this audit found are: Graft unreachable on this machine; no semantic search over prose; and `MEMORY.md` at 21,867 of a 24,400-byte cap. None of those is solved by adding the five other components.

## The decisive test: does any regime save turns or tokens on real questions?

Run 2026-09-16 on the subscription, no API. Sixteen questions with ground truth verified beforehand, four each from the roadmap loop, the skills and governance layer, contract cite-grounding, and code navigation. Each question was answered by a fresh Sonnet subagent under four regimes whose prompts were byte-identical except for the tool block: R0 baseline (`rg`, Read, Grep, Glob, `just overlay-query`), R1 Graft added and "try first", R2 the Pixeltable prose index (670 files, 42,046 chunks, bge-small on MPS) added and "try first", R3 the per-file Graphify doc graph plus the whole-repo code graph added and "try first". Scored on verified correctness, tool calls, wall clock, and total tokens; tool use was read from transcripts, not from the agents' own counts. Raw rows: `~/.claude/jobs/8cde37b5/tmp/results.tsv`.

| Regime | Correct | Mean tool calls | Mean wall clock | Mean tokens | Preferred tool actually invoked |
|---|---|---|---|---|---|
| R0 baseline | 16 / 16 | 3.00 | 14.8 s | 75,635 | n/a |
| R1 Graft | 16 / 16 | 3.00 | 17.0 s | 76,534 | 8 of 16 |
| R2 Pixeltable | 15 / 16 | 3.44 | 26.6 s | 77,933 | 11 of 16 |
| R3 Graphify | 16 / 16 | 3.44 | 13.6 s | 77,760 | 4 of 16 |

Every run pays about 74k tokens of subagent preload before the first tool call, so the regimes differ by one to four thousand marginal tokens, within noise. Three findings survive that noise.

**The baseline is already near the floor.** On this repo's question shapes, `rg` plus the overlay answered 16 of 16 in a mean of three tool calls and fifteen seconds. There is no turn count for a retrieval layer to cut. The one question the baseline found expensive, listing the five production callers of a hash function, took six calls and 39 s; Graft answered it in three calls and 27 s with `graft callers`, and Graphify in five calls and 25 s. That is the whole measured benefit of the code graphs: exact call edges on the one question shape grep handles badly.

**The prose index cost accuracy on code.** R2 was the only regime to get a question wrong: its subagent ran the index on the caller question, got prose hits, then grepped incompletely and reported three callers of five. The index is prose-only by construction, and its 9 s cold start (Postgres plus model load) shows in R2's wall clock, the slowest of the four. On document questions it landed the right file every time it was used, but so did grep.

**Agents do not adopt a tool because a prompt says "try it first".** With an explicit instruction, Sonnet invoked Graft on 8 of 16 runs, the index on 11, and Graphify on 4. On document questions it mostly went straight to grep and was right to. Adoption tracks whether the tool fits the question shape, which means any of these tools earns its keep only where grep is weak, and the prompt cannot manufacture that.

**Verdict for the decision.** Adopt Graft, because it is already integrated, costs nothing to keep current (11 s incremental), and is the only regime that measurably beat grep on a real question shape. Do not wire the Pixeltable index into the agent path as a first-call tool; keep it as an on-demand search for paraphrased prose questions, where the earlier pilot showed 5 of 5 section-level hits that grep cannot produce, and never for code. Do not adopt Graphify for agent retrieval on this repo; its per-file Haiku graph is accurate but agents reached for it in a quarter of runs and gained nothing over the baseline when they did. A larger, harder question set (paraphrased governance questions, cross-document relation questions) could move these numbers and is the next measurement if one is wanted; the sixteen here are the shapes that actually recur in this repo's sessions.

Two caveats on the design. The subagents' 74k preload is a fixed cost of the harness, not of any regime, and a longer-running agent that keeps a tool's output in context would amortize differently. And the Graft runs launched before the binary was on Claude Code's PATH were re-run, but two of the original runs whose agents never tried Graft were kept as valid non-adoption observations.

## Eight applications beyond retrieval: evaluated

After the decisive test the operator asked what else the stack could do for this repo's work, roadmap, skills and loop, and then asked for durable evidence on each. Eight evaluations live at `docs/research/context-stack-evals/` with their scripts, raw results and an index (`README.md`) that carries the verdict table. In one paragraph each:

- **E1, duplicate findings in the review loop: supported.** Embedding 2,092 merge-gate findings shows recurrence at the class level in 16 of the 20 most recent (2 are the same defect re-found), and one of the E2 reviewers re-found a shape already recorded four times on the ledger. An index that shows a reviewer the sibling finding before it re-reports is the cheapest lever this set found.
- **E2, structural packs for transcript-less reviewers: not supported on these PRs.** Four fresh reviewers over two merged PRs, with and without a Graft blast-radius pack, found one verified finding with the pack and two without, on both PRs. Both packs truthfully said nothing depends on the diff; tool-and-test PRs give a blast radius nothing to show. Untested where it could matter, on `harness-*/src` changes with real callers.
- **E3, cross-spec drift by embedding: not supported as configured.** 76 candidate pairs above 0.90 across the governance corpus, and the top fifteen are all intentional: delta-chain boilerplate and relocation headers. A drift detector needs a definition of drift and boilerplate exclusion before the ranking means anything.
- **E4, session history as a memory tier: partially supported.** 15,214 chunks from 243 sessions; a memory's one-line description finds its origin session at hit@5 for 14 of 30. Half the facts the memory system preloads are re-findable on demand from a weak query.
- **E5, DuckDB over the ledgers: supported as analytics.** Millisecond queries over arc-metrics and the 1.3 MB register. The cohort numbers differ from the lever report's because that tool's exclusion rules are its substance, so any port carries them as predicates.
- **E6, Obsidian for the operator: artifact only.** A 619-note, 2,114-link vault of the governance graph is built; whether it helps is the operator's test.
- **E7, card brief versus full context for subagents: supported, and the strongest number here.** On the clean question pairs a Graft-map brief halved first-turn context (about 28k versus 56k tokens) and total tokens (88k versus 162k) at identical correct answers. Two full-context runs were consumed by the repo's own Stop-hook lint gate, which is its own finding about in-repo agents.
- **E8, oracle for the product memory substrate: not a fit.** C-MEM-11 ranks by metadata rules with a deterministic trace, not similarity; the required verification is property tests the repo already writes.

Two hazards surfaced during the runs and are recorded in the index: `graft build` in-repo rewrote four tracked files including `.claude/settings.json` (restored from HEAD), and Graft's self-reported token savings are not evidence.

## Proposed sequence

*Written before the decisive test; steps 1 and 3 were executed during it (Graft restored via symlinks into `~/.local/bin` and `/opt/homebrew/bin` plus the package into Homebrew's global `node_modules`; the prose index built over 670 files). The decisive-test verdict above supersedes the ordering below where they differ.*

1. **Restore Graft (operator prerequisite, minutes).** Install it where Claude Code's `node` can find it: `npm i -g @nanonets/graft` with the Homebrew npm, or symlink the mise binary into `~/.local/bin`. Then `graft build`, confirm the MCP server connects at next session start, and confirm `graft-hooks.cjs` finds the package. This alone reinstates the existing integration.
2. **Measure the deep layer on a subtree.** `graft build --deep` against Ollama (`--provider openai --base-url http://localhost:11434/v1 --model llama3.1:8b`) on `harness-core/` first; time it, read five summaries, decide whether the whole 23k-node pass is worth the wall clock.
3. **Pilot the prose index.** Pixeltable table plus chunk view plus Ollama embedding index over `design-substrate/*.md` and `docs/governance/*.md`, adapted from `winning-defense/scripts/build_index.py`, surfaced as a `just doc-search "<query>"` recipe. Acceptance: on twenty real cite-grounding questions from recent sessions, it finds the right section faster than `rg` in most cases. No hook wiring until it passes.
4. **Only then** decide on DuckDB (if a ledger query appears) and Obsidian (if you want to read the vault).

Everything above is mode-agnostic workspace tooling under root `CLAUDE.md` §11.2 and lives in `tools/`, `.claude/`, `justfile` and gitignored index directories. It must not touch `design-substrate/**`, must never present an index as authoritative over HEAD, and must not be pitched as preload reduction, which the operator closed at Floor B.

## Not verified

- Pixeltable's cell-level recompute for arbitrary computed-column chains (documented only for the embedding-index path).
- Runtime of `graft build --deep` over 23k nodes on Ollama.
- Graphify doc-layer quality beyond one 8-file run per backend; the frontier cost range assumes a direct API backend at 1–1.5× document tokens, not measured.
- torch wheels for Python 3.14 (3.12 verified by dry-run; the project venv is 3.14).
- Graphify's exact Obsidian frontmatter schema.
