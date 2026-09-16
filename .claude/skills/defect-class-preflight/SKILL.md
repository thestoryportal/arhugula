---
name: defect-class-preflight
description: Pre-commit self-review sweep against the recurring defect classes this workspace's reviewers actually catch, distilled from the merge-gate/codex finding corpus (the corpus only grows; scripts/refresh-classes.py rederives counts). Use BEFORE every commit of code in an arc — after writing or modifying any code under tools/ or harness-*/ and before invoking `just review-with-failover` or the merge-gate. Also use whenever about to claim a fix is complete, whenever a diff touches a shared surface (env variables, hooks, conftest, constants), whenever a review round's fix is being committed (fixes introduce their own defects), and whenever a diff introduces a new consumer of an existing data surface — another tool's log/ledger/store/output, an env variable, or an external SDK — which fires the new-consumer inventory pause at authoring time, BEFORE the consumer is written. One pass here is how a first draft survives review — skipping it is how arcs run 9–17 BLOCK rounds.
---

# defect-class-preflight — sweep the diff before the reviewers do

## Why this exists

This workspace's review loop is fail-closed: every defect a reviewer finds costs a full
serial cycle (fix → probe → commit → CI → re-review → re-bind), typically 15–30 minutes.
Analysis of the recorded findings in `.harness/merge-gate-log.jsonl` (1,084 at first distillation, 2026-08-24; the committed corpus below is newer) shows they
cluster into a small set of recurring classes — and repeatedly, the defect found was a shape the
workspace had *already fixed once elsewhere* (the same two-armed restore shipped twice in
one arc). The knowledge exists; this skill is its activation at authoring time. One pass
here converts review rounds into pre-commit edits.

## How to run the sweep

Scope it to the diff, not the repo: `git diff HEAD` (or `--cached`) plus any new files.
**Step one, always: run `scripts/preflight-grep.sh`** — it flags high-signal textual
shapes, and every hit demands a NAMED answer in your sweep (its silence proves nothing;
it exists because instruction-following alone measurably misses shapes the grep cannot:
the two-armed save/restore was named in only 1 of 4 skill-eval reviews until it was
mechanized). Then, for each changed hunk, walk the classes below **in order** — they
are ranked by real finding frequency — and answer each applicable class's question
concretely (name the line, not "looks fine").

Three meta-rules that outrank the list:

- **A fix you just wrote is the least-reviewed code in the arc.** After absorbing a
  review finding, sweep the fix itself with this list before committing — measured
  across recent arcs, roughly a third of findings were introduced by a previous round's
  fix, and on the u-he-35 arc 8 of 29 findings landed on code its own absorptions had
  added (charter WR-04, [A] §2).
  The place this rule is actually lost is the sweep ANSWERS. You will have spent the
  last hour inside the reviewer's finding; the work will feel like a repair rather
  than a build; and you will write *"no new mechanism"* into the answers file without
  re-reading what you just added. That is the moment. **A commit that adds a mechanism
  can never answer "no new mechanism"** — the u-he-35 r5 sweep answered exactly that
  while introducing `_LiveGroups`, whose race came back as r6's finding one full round
  later. An answers file carried over from the pre-absorption sweep describes the diff
  you MEANT to write, not the one you are committing: re-run
  `scripts/preflight-grep.sh` over the absorption's own bytes and answer from its
  hits.
- **Every numeric bound names the contract value it derives from.** Any literal in a
  guard, allowlist, validator, or budget — a range, a cap, an arity, a retry count —
  is either traceable to a contract value you can cite, or it is a guess wearing a
  validator's uniform. Put the derivation where the number is, not in the PR body.
  The rationalization sounds like care: *"1 to 99 is obviously reasonable — nothing
  legitimate falls outside it."* Reasonable-looking is the defect. The u-he-35
  guard-reps token took FOUR paid touches — `any` → `1–99` → `1–9` → `5–9` — because
  each bound was invented from how the line read instead of from what the contract
  says, and each invented bound bought exactly one more round in which to invent the
  next (charter WR-05, [A] §3). Nor is a wide bound the safe default it looks like: a
  range that admits values its contract forbids is a gate that claims to enforce and
  does not — class 12's shape with an integer in it. `scripts/preflight-grep.sh`
  mechanizes the argparse half (`type=int`); this rule covers every literal the grep
  cannot see.
- **Touching a shared surface obligates a blast-radius pass first**: `graft callers
  <symbol> --depth 2` (or `--depth all` for renames/precondition changes) plus a grep
  for every consumer, then read each consumer's *semantics*, not just its existence.
  The costliest P1 in the corpus was a variable that named TWO authorities — the
  change was correct for one consumer class and destructive for the other.

## The new-consumer inventory pause (authoring-time — fires BEFORE the sweep)

Everything else in this file runs at pre-commit. This section runs earlier: the
moment you are ABOUT to write code that reads a data surface some other code owns.
Trigger: the diff introduces a NEW consumer of an EXISTING data surface — another
tool's log, ledger, store, or output file (`.harness/merge-gate-log.jsonl`,
`.harness/substitutions.yaml`, reservation records), an env variable someone else
writes, a JSON/YAML schema owned elsewhere, or an external library/SDK's API. When
that trigger fires: STOP. Do not author the consumer yet.

Why the pause pays: in one recent arc, roughly 13 of the first 20 reviewer findings
were serially-discovered producer contracts — "that field can be null," "absent on
legacy rows," "the writer can die mid-record" — each surfaced one at a time, each
costing the same full serial cycle "Why this exists" prices at 15–30 minutes. One
up-front inventory table would have converted all of them into pre-commit edits.
Letting reviewers read the producer to you, one field per round, is the most
expensive possible way to read it.

The rationalization you will hear yourself think, mid-arc, is: *"the field obviously
exists — I'll just read it."* Treat that exact sentence as the tripwire, not a
waiver. Every one of those ~13 findings was a field that obviously existed; what did
not obviously exist was its null case, its absent-on-old-rows case, its
half-written-at-crash case, its provenance. The field name is the visible tenth of
the contract — the pause exists to surface the other nine tenths before a reviewer
does.

**The inventory.** For EVERY field the consumer will read, establish the producer's
semantics — not the schema you assume, the semantics the writers actually implement:
can it be null? absent entirely (older rows, optional emit paths)? partially
written — what does a mid-crash write leave on disk? what provenance / generation /
versioning does it carry, and must the consumer honor it? which `C-*` contract (if
any) governs it? Two dimensions field-level inventories measurably miss (u-he-33,
2 P1s): **venue semantics** — which interpreters/venues can IMPORT or reach the
producer module at all (a 3.12-only producer consumed from a stdlib-3.9 venue
silently no-ops every downstream check), and **lifecycle semantics** — where the
data goes when its carrier ends (a lease's `unblocked_from` had to be found again
in the moved-aside `released.*` records after release; the live object is not the
record's whole life). Instruments: `graft callers` / `graft grep` on the producer symbol
to read every WRITER's semantics — the meta-rule's blast-radius pass, pointed the
other way — and `just overlay-query` for the `C-*`/`U-*` contract cites. Record the
answers in a TABLE, field × semantics. That table IS the test matrix — and the unit
is the recorded SEMANTIC, not the row: each field × each recorded semantic (null,
absent, partial, provenance, …) is its own case the consumer must survive, and
testing one variant does not retire the field's others. A recorded semantic without
a test is a reviewer finding on layaway.

**Parse, don't validate.** A data-surface consumer gets a typed row model — Pydantic
v2 in this workspace — at the boundary; illegal shapes are rejected ONCE, at parse
time, and everything downstream handles only legal ones. The shape to refuse:
`dict.get()` chains scattered through the logic, each one a private, unreviewed
theory of the producer's contract. Ten `get()` sites are ten places the theory can
be wrong; one row model is one.

**Precedent search.** Before writing the new consumer, read how the surface's OWN
tooling already handles these fields — its existing consumers, the producer's own
reader. The precedent usually already answers the null/absent/partial questions
(someone paid for those answers in review rounds once). Match it, or name in the
diff why you diverge — a silent divergence from the surface's own reader is a
finding either way.

**External-SDK half.** When the consumed surface is an external library/SDK rather
than an in-repo file, the same pause applies, with a three-rung ladder:

1. **Run the interface** — actually call the API/CLI and look at REAL output.
   MANDATORY wherever a read-only, free probe exists, and never replaced by any
   rung below: every `resp["items"][0]` written unprobed is a checkable claim
   about a shape you have not seen. When the only available probe would be paid,
   credential-gated, or mutating, do NOT fire it — the standing
   no-unilateral-paid-calls rule wins: complete the pause on the remaining rungs
   plus the producer-source read, record the unprobed shape as a NAMED gap in the
   inventory table, and surface the probe itself to the operator gate.
2. **Read the resolved installed source** — the zero-cost middle option. FIRST
   resolve what actually runs — the real import path/executable and its version
   (`python -c "import x; print(x.__file__, x.__version__)"`, `which <cli>` plus
   `--version`, or the environment's own metadata) — THEN read that source. The
   repository `.venv` is the common landing spot, never an assumption: a stale
   `.venv` beside a `uv run --with` environment, a system package, a Node SDK, or
   a global CLI all read fine and inventory the wrong producer — source read at
   the wrong version is a wrong-contract inventory wearing the right one's
   clothes.
3. **context7 MCP** for current upstream docs — configured in the workspace
   `.mcp.json`; its tools are deferred, so load them via ToolSearch before
   calling. In a runner without context7 (the Codex bridge, say) the pause
   completes on rungs 1–2 — this rung is an accelerator, never a dependency.
   context7 supplements the probe, never substitutes for it: a probe witnesses
   ONE instance, so it falsifies a doc claim only for the shape it actually
   received — a doc- or source-derived variant the probe did not witness STAYS
   in the inventory table as a case to handle; the probe narrows nothing it did
   not see.

## The mechanism-precedent search (grounding-time — fires BEFORE the first line of a new tool)

The new-consumer pause above covers DATA surfaces. This step covers MECHANISM
surfaces, and it fires earlier still — at grounding, the moment you can list which
mechanisms the new tool needs (subprocess lifecycle, signal handling, file
publication, verdict parsing, guard wiring) and have written none of them. For
each mechanism on that list:

1. **Read the reviewed sibling that already does it — then adopt its shape or
   import it outright.** At the 2026-08-26 audit [A] the sibling wrappers
   (`run_bounded`/`terminate_bounded`, `agy_review`, the guard-allow commits)
   carried ~90 absorbed findings of reviewer pressure: they are the reviewers'
   standard in executable form, the local bar. Diverge only with the divergence
   named in the diff — a silent divergence from the reviewed sibling is a finding
   either way, same rule as the data-surface precedent search above.
2. **Grep the gate-log corpus for that surface** (`.harness/merge-gate-log.jsonl`):
   *what has this reviewer said about code like this?* The findings absorbed on
   the sibling's own arc are the exact rounds you are otherwise about to re-buy.
3. **Treat the plan skeleton as UNREVIEWED input.** It sketches shape; it never
   grants contract. Where it contradicts a spec phrase or a reviewed sibling, the
   sibling/spec wins and the skeleton is the finding (class 12's plan-over-spec
   deference — both u-he-35 P1s were exactly this).

The price of skipping, measured (charter WR-11, [A]): at least 7 of the u-he-35
arc's 29 findings re-derived — one paid round at a time — disciplines the sibling
wrappers already embodied. The rationalization arrives as *"my tool's case is
different — faster to just write it."* Whether it is different is precisely what
the sibling read establishes; unread, "different" is a guess priced at one review
round per mechanism.

## The classes, ranked by finding count

*(Counts and order for classes 1–10 regenerated from the committed log by `scripts/refresh-classes.py` at the 2026-08-24 committed corpus of 1,095 findings — rerun the script for live figures; the log only grows, so these are a bound snapshot, not live state. Classes past 10 are appended by the arc that paid for them and carry that arc's count, so they are outside the ranking.)*

### 1. Race / TOCTOU / atomicity (379 findings)
Any check-then-act on files, refs, locks, or shared state. Ask: *between my check and my
act, what can another lane, process, or signal do?* Shapes: absence-check then create
(use exclusive create); read-modify-write without CAS; cleanup racing a writer;
`mkdir` without `exist_ok` semantics thought through; rename/unlink where a crash
mid-sequence leaves a half-state. This workspace's idioms: exclusive-create CAS
(reservations), temp-then-`os.link` publication, single-writer leases. If your diff
adds coordination, name which existing idiom it uses — a new hand-rolled one is a
finding waiting to be filed. And any file a privileged or auto-allowed path READS or
WRITES that another actor can pre-plant gets the containment idiom: open with
`O_NOFOLLOW|O_NONBLOCK` + post-open `fstat` `S_ISREG`, refuse symlinks/special files
loudly, publish via same-directory temp + `os.replace` (the finding_record/merge_door
hardening; missed unfired on the B-215 gate's state file, codex r1).

### 2. Prose that will drift (151 findings)
Docstrings, comments, and `.harness` prose containing checkable facts: counts, line
numbers, §-cites, "all/every/only" absolutes, arithmetic that implies a partition.
Rule: a fact checkable against HEAD does not belong in prose unless bound to a commit
or a round ("six witnesses" was wrong twice in one arc; the census "6288+82" implied a
false partition). Fix at authoring: delete the count, bind the claim, or verify the
cite by reading the cited section *now*. (Full discipline: the `register-pr-prose`
skill.)

### 3. Silent failure / meaning-changing fallback (114 findings)
`2>/dev/null`, `|| true`, `except: pass`, a default that changes meaning when the
primary path fails, an empty result indistinguishable from "could not look". Ask of
every error path: *if this fails at 3 a.m., does anyone find out, and does "empty"
mean empty or unlooked?* A gate that can't tell those apart must fail loud. The
shape that got past this wording for THREE consecutive review rounds (u-he-33): a
helper whose `except`/error arm returns the success-shaped empty (`return []`,
`return None`, `return set()`) — every one of those is this class wearing a return
statement; route the failure to ONE loud enforcement point instead. Rider for
DETECTION/ENFORCEMENT surfaces: any input that can SUPPRESS a check (an attestation
set, an exemption list, an allowlist, a dedupe key) is itself attack surface —
sweep it for forgeability and containment (symlinked dirs/files, schema-shaped
forged entries) before trusting it to mute anything.

### 4. Vacuous witness (107 findings)
For every new/changed test, reason the mutation through before committing: *if the
load-bearing line were deleted or inverted, does this test actually red?* Traps seen
repeatedly: presence-check standing in for behavior (asserting a variable is set, not
that the writer honors it); a witness that dies before reaching its discriminating
assertion (KeyError before the emit); a child process that never instantiates the
thing under test; an assertion after an early return; and, in a doc or prose witness, a
needle on a list marker, heading or label (`- **K5 —`, a bare `checkpoint`) instead of the
claim text after it — invert the claim, keep the marker, and the test stays green (U-HE-39,
merge-gate witness lens r1: K5, K7 and K8 of the four K-dispositions pinned only by their bullet). For a
doc witness the mutation to reason is *negate the sentence*, not *delete the line* — and a
claim-text needle that stops mid-sentence is the same defect one clause later: pin the WHOLE
claim, matched against the file with its whitespace flattened so a wrapped sentence is one
string (U-HE-39 r2: K7's "shadow mode only" clause sat outside a first-half needle). Where
feasible, actually run the
mutation probe (`just mutation-probe`) rather than reasoning it — and probe the fix
BOTH ways (kill confirmed, then green restored).

### 5. Timeout / retry / budget arithmetic (93 findings)
Any bound, retry, or budget: sum the worst case and compare against every enclosing
bound (three retry budgets inside a 30 s waiter summed to 33.9 s). Unvalidated
environment-sourced budgets, wall-clock assertions that breach under load (assert the
SHAPE — call counts — not the milliseconds), sleeps standing in for synchronization.

### 6. Unreachable / dead branch (75 findings)
Every `else`, `except`, and restore arm: *what call sequence reaches this?* The
recurring shape: a hand-rolled two-armed save/restore where one arm is unreachable
from any real caller. It hides in EVERY syntax — an `if saved is None: pop / else:
set` pair reads the same whether it sits in a bare function, a `finally:` block, a
context manager's exit, or a fixture's post-`yield` tail; sweep every save/restore
PAIR by its shape, not by where it appears (an eval reviewer holding this very
checklist walked past one wrapped in `try/finally`). Preferred fix is deletion via a
mechanism that owns both modes (`MonkeyPatch`, a context manager) — delete the branch
rather than contriving a witness for it.

### 7. Env-var mutation and restore (73 findings)
Any `os.environ` write: who restores it, does the restore survive a mid-test
`monkeypatch.undo()` (use an INDEPENDENT `MonkeyPatch`), does it leak into suites that
assert the namespace empty (`HARNESS_*` must never escape tools items), and — the P1
shape — does the variable serve MORE THAN ONE consumer meaning? Enumerate every reader
of the variable before repurposing it.

### 8. Subprocess boundary (58 findings)
Monkeypatching a Python seam cannot reach a child process — only inherited env can.
Children get a COPY of env at spawn; later parent changes don't propagate. Nested
sessions of the same tool (pytest-in-pytest) re-run your own hooks against
already-modified state — first-writer-wins any value that must survive nesting.

### 9. Path / default resolution (56 findings)
Any path computed from env-or-default: TRACE the chain to the concrete path it lands
on when NOTHING is set, and write that path into your review — "falls back to
`~/.reports/` (the operator's real store) when the env var is unset" is a *named
finding*, not an implementation default to read past. Def-time constants bake the env
at import (`QUEUE_DIR`) — a later env change does not reach them. A worktree does NOT
isolate `$HOME`-absolute paths; isolate by ENVIRONMENT.

### 10. Fixture scope / lifecycle phase (35 findings, but two P1s)
pytest specifics that shipped defects: a per-item bracket covers setup+call+teardown
but NOT collection/import time or session-fixture teardown after a foreign final item;
higher-scope teardown runs inside the CURRENT item's teardown phase (measured);
`conftest` runtest hooks fire for items ANYWHERE in the run unless path-guarded; a
function-scoped autouse fixture covers only the test body.

### 11. New authority-bearing command surface (added U-HE-47; 5 findings in one arc)
Fires the moment a diff introduces a NEW command, verb, or recipe whose effect can
mute, dispose, suppress, or authorize anything (an adjudication, an exemption write,
a gate override). Before committing, answer ALL FOUR — every U-HE-47 review round
2–5 finding was one of these, discovered serially at a full round each:
(a) **Guard venue**: how does the permission guard see it? A generic-prefix allow
auto-approves every argument shape — an authority-bearing verb needs an exact-shape
validator (arity-bounded, enum-pinned submodes, identity-pinned actors), with the
dangerous submodes left at ask. (b) **Authority half**: the guard validates FORM
only — what binds the CLAIM to real state? Caller-supplied text (an env prefix, a
flag value) is not authority; bind to holder/reservation/store state the caller
cannot choose (the record_phase pattern). (c) **Carrier parity**: every runner's
carrier (.claude skill, .agents bridge/projection) must document the SAME invocation
the guard actually allows — a bare documented form against a prefix-requiring guard
strands headless at ask; identities parameterize per runner. (d) **Production-call
witness**: a static test must pin the documented command shape in each carrier —
helper-level tests stay green when the production instruction is reverted.
**Sweep-altitude rule (U-HE-47 r2→r5, four rounds on one verb):** at the FIRST
reviewer finding on such a surface, do not fix only the named token — enumerate
EVERY degree of freedom of the command (each argument, each identity, each env
input) in a table with its pinning authority (enum, identity set, holder state,
ask-gate), and close them ALL in that absorption. The sibling sweep's unit is the
mechanism's whole authority surface, never the literal flagged shape — otherwise
the reviewer walks the remaining dimensions one full round each.

### 12. A quoted contract phrase with no line behind it (added U-SR-01; both of the u-he-35 arc's P1s)
Fires whenever the diff QUOTES a contract — a spec phrase in a docstring, a
requirement copied into a verification-manifest row, a comment restating what the
code guarantees. Ask it per phrase: *which line discharges this?* Name that line. An
answer of "the mechanism generally does that" means the phrase is undischarged, and
an undischarged phrase is another finding on layaway — the wording of a guarantee
sitting in the place the guarantee was supposed to go, and reading, to every later
reviewer, exactly like the real thing.

Both P1s of the u-he-35 arc were this shape, and both were spec-verbatim misses at
turn 0 — the deciding text was in the session's own context before the first line
was written:
- `codex_review.py`'s own docstring said "the exit code is a convenience, never a
  verdict," and the loop skill body said a verdict counts only on its schema parse
  (C-HE-15), never on exit code or silence. `returncode in (0, 1)` shipped as the
  verdict anyway.
- The spec's "result row required before pilots" was read at grounding and copied
  verbatim into the arc's own manifest row. Nothing enforced it. Four rounds
  (r1, r5, r9, r10) went to re-litigating a gate the row already claimed.

The rationalization arrives in the voice of diligence: *"the plan skeleton already
shows `returncode in (0, 1)` — I'll follow the plan."* That is plan-over-spec
deference. A plan skeleton sketches SHAPE; it never grants CONTRACT, and where the
skeleton and the quoted phrase disagree the phrase wins and the skeleton is the
finding.

Two exits, and only two. **Discharge it** — write the line number beside the phrase.
Or, if you believe the phrase is wrong, **route it**: a quoted spec requirement is
BINDING, so disagreeing with it is a Class 1 fork to design-phase back-flow, not an
edit you make on your own authority. What is never an exit is deleting the quote and
describing what you built instead: the requirement outlives the sentence, so that
move leaves the violation in place and removes the only evidence of it. Both P1s
above stay defective under it — exit-code-as-verdict is still wrong once the C-HE-15
quote is gone, and the pilot gate is still unenforced once the row stops claiming it.
Silent absorption is the worst failure mode this workspace has; a docstring is a
cheap place to commit it.

### 13. A new command the loop must reach (added U-SR-01; u-he-35 r2, one round)
Class 11 asks what a new verb can AUTHORIZE. This asks the other half: can the loop
INVOKE it at all? The trigger is mechanical — the diff adds a justfile recipe (or any
new command shape) whose verification-manifest `runs_in` includes "loop" — and equally a
diff that CHANGES a documented command shape in a carrier to fix what the command records
(an added env prefix, flag or argument): that fix is only as durable as the pin on it
(U-HE-39 witness lens r3: a `HARNESS_LANE_ID=` prefix added to six carrier lines shipped
unpinned, and the one existing assert matched the command with or without it). Answer
both halves: is the permission guard wired to auto-allow the EXACT shape the loop
will type, and does a witness pin that shape so reverting the wiring goes red? The
precedent commits already model the whole chain — recipe ⇒ guard allow ⇒ witness
(U-HE-25, U-HE-34); match one of them instead of inventing a fourth arrangement.

The temptation is the harmless recipe: *"it only publishes a log — there's nothing
dangerous here for the guard to gate."* Danger was class 11's question, and the two
questions come apart precisely here. The guard's silence on a harmless verb is not
permission; it is an ask prompt — and an ask prompt inside a headless lane is a
stall with nobody awake to clear it.

### 14. Signal handler meets lock (added U-SR-01; u-he-35 r10)
Any signal handler in a process that also takes locks, or any handler touching state
a lock guards. The fact that decides the answer: in CPython a Python-level handler
always runs on the MAIN thread, whichever thread the signal was delivered to. So ask
*which* thread holds the lock when the signal lands, because the two arms fail
differently:
- **Main thread holds it** — the handler is the lock's own owner. An `RLock` lets it
  straight back in and the handler walks into the middle of a half-finished update
  the lock existed to hide; a plain `Lock` deadlocks against itself instead.
- **A worker holds it** — the handler is not the owner, so it BLOCKS, waiting inside
  a signal handler for a lock it cannot hurry. If that worker needs anything the main
  thread was going to do, the wait never ends.

The trap is assuming one arm is the whole story: an `RLock` looks like protection
against the first and buys nothing against the second. Same family as the recorded
fork-while-holding-a-lock deadlock — the hazard is asynchronous control transfer
while a lock is held, whichever way control goes.

*"The handler only sets a flag"* is the sentence to distrust — first because the
flag's readers may assume it changes only between guarded sections, and second
because the handler that only sets a flag this round grows a cleanup call the next.
Keep handlers to what is async-signal-safe (set a flag, write one byte to a
self-pipe) and let an ordinary thread do the work under the lock.

### 15. Bound bytes are not the executed bytes (added preflight-finding-intake; 10 rows at the 2026-09-16 corpus, 2 of them this exact shape)

A check, reviewer, or classifier runs against the **working tree** while the record it
produces binds the **committed** `base..HEAD` range — so an uncommitted edit can make
the check pass, the attestation records HEAD, and the edit is then reverted or never
lands. The 2026-08-19 wrapper finding ("the reviewer runs against the live worktree
while the binding covers only the committed diff") and this arc's own codex r1 P2
(the class table read from the tree while the sweep attestation bound HEAD) are the
same defect. **Question:** *for every artifact whose record names a binding, do the
bytes it actually read come from that binding (`git show HEAD:<path>`, `base..head`
diff), never from the tree?* If a test can make the check pass by writing a file
without committing it, this is the class.

## After every review round — the class-sibling sweep (before the next invocation)

A reviewer finding names an INSTANCE; the absorption owes the CLASS. Measured on the
u-he-33 arc: the returns-empty-on-exception shape was found in one helper in round 2
and then re-found in two sibling helpers across rounds 4-5 — two full rounds spent
re-discovering a class already in hand. So, after adjudicating a round's findings and
BEFORE re-invoking the reviewer:

1. Classify each finding into the classes below (or the pause's dimensions).
2. Sweep the ENTIRE diff for other instances of that class — grep the shape
   (`except`, `return []`/`return None` on error arms, the suppression inputs, the
   bare counts), then read each hit's semantics. Fix siblings in the SAME absorption
   commit; the reviewer should never meet the same class twice in one arc.
3. Adjudicate each absorbed finding on the gate log (C-HE-24 §5, U-HE-47): for every
   finding row this round produced, once its fix is committed (or it is refuted with
   grounds, or it is held per step 4), append the disposition —
   `HARNESS_ARC_ID=<arc-id> just merge-gate-adjudicate --finding-id <id>
   --disposition accepted|rejected --actor <runner>_absorber` (`accepted` = the fix
   was applied; `rejected` = refuted; the finding_id is on the round's emitted JSONL
   rows). EVERY finding gets one of these two, always: an absorber writes no third
   state, and no finding may be attested past with `disposition=null` — see step 4
   for why holding is not the exception it looks like. The
   `HARNESS_ARC_ID=` prefix is
   REQUIRED — the guard auto-allows only the prefixed form, and the CLI holder-binds
   it to this lane's live reservation. The actor is the RUNNER's own absorber
   identity — `claude_absorber` on the Claude runner, `codex_absorber` on the Codex
   bridge — never the producer, never `operator`. Without this row the finding stays disposition=null forever and N6 counts
   nothing — the attest below records that you ANSWERED the finding, never that it
   was DISPOSED.
4. **A finding you HOLD owes a fail-closed probe in the same round.** Holding is the
   cheapest-feeling disposition in the loop, and it arrives sounding like good scope
   discipline: *"that's a later unit's job — the plan already schedules it."* The
   scope call is often right. What is never right is holding it BARE. In the same
   round, land the minimal thing that fails loud when the held condition is violated —
   one assertion, one refusing row, one guard — and the hold becomes a scope decision
   with a floor under it. Held bare it is a promise, and the reviewer does not take
   promises: the u-he-35 pilot gate was held at r1 and then re-litigated at r5, r9,
   and r10 — four paid rounds on one unpromoted hold, the costliest policy miss of
   that arc (charter WR-06, [A] §1).

   **"Held" is not a ledger state, and reaching for one is the error.** Both obvious
   moves are wrong, and they are wrong in opposite directions. Writing `suppressed`
   names its actor as the *adjudicating authority*, which C-HE-24 §5 restricts to a
   decorrelated lens, a deterministic rule, or a logged operator override — an
   absorber is none of the three, so the row asserts an authority that never existed.
   Leaving the row null and naming the finding in the sweep is worse in a quieter
   way: `unanswered_findings` subtracts every id an attestation names, and it never
   reads disposition, so the obligation disappears permanently while the ledger still
   says nothing was decided. One move fakes a verdict; the other loses the finding.

   The way out is to notice that the probe already IS the disposition. If you landed
   the fail-closed floor, you FIXED this finding — the risk it named is now caught —
   so it is `accepted`, and what remains is not this finding at all but a separate
   scope item: the policy, the real ceiling, the proper owner. Register that as its
   own forward row where forward work lives. A reviewer's finding and the scope it
   brushes against are two objects, and collapsing them is what made "held" feel
   necessary.

   And if you cannot land even the minimal probe, you have no disposition to write —
   so do not attest past it. That is the genuine operator gate: halt, surface the
   finding, and let the operator direct the fix or write the `suppressed` row on
   their own authority, which is the logged override the contract names. The
   permission guard's `accepted|rejected` allowlist (`_adjudicate_exact_shape`,
   U-HE-47) is that boundary made mechanical; never widen it to get past this moment.

5. If the class was absent/unfired in this file, repair the skill in that commit too
   (the loop below).
6. When two consecutive rounds' findings target mechanisms YOUR absorption invented
   (not the plan floor), stop hardening and re-scope by subtraction — the recorded
   adversarial-hardening arms race does not converge by adding layers.

## When a reviewer catches what this sweep missed — the skill's own repair loop

This checklist is a distilled map of `.harness/merge-gate-log.jsonl`; the log is the
authority and only grows. Passive knowledge decays (a rule recorded in memory failed
to prevent the same defect days later, in the same arc that wrote it), so the repair
obligation is IN-COMMIT, not remembered:

When absorbing any reviewer finding this sweep should have caught, classify the miss
and repair the skill **in the same absorption commit**:

- **Absent** — no class covers it → add the class (or extend one), with its question
  and the real example.
- **Unfired** — the class exists but its wording didn't trigger on this shape → rewrite
  the item so the shape is unmistakable (class 6's `finally:` note exists because of
  exactly such a miss).
- **Overridden** — the sweep flagged it and the flag was argued past → the text is
  fine; record the override in the arc's register instead, and consider promoting the
  class DOWN the activation ladder (into `scripts/preflight-grep.sh`, a ruff/semgrep
  rule, or a CI check) — mechanical firing beats instruction-following for classes
  that recur despite being written down.

Then add the miss as a planted-defect case in the skill's eval set
(`evals/evals.json` in this skill's directory), so the repair is
regression-tested like code. The skill file is tracked; its git history is the audit
trail — no separate ledger.

**The loop is enforced at the sweep attestation, not remembered.** This obligation
executed zero times across the ten absorption commits of U-HE-35 while it lived only
in this prose (charter §1, "nothing flows in"). So `just review-template-sweep`
classifies every outstanding reviewer finding against `scripts/refresh-classes.py`'s
table (its `classify` verb) and stamps the result above each finding; a finding that
matches NO class is marked `UNMATCHED` and pre-filled with an `intake:` slot that
`just review-attest-sweep` refuses to leave empty. Exactly one of:

- `intake: class-extended <class-number> <what changed>` — you extended a row in
  `refresh-classes.py` (and this file) so the finding now matches;
- `intake: new-class <name> <what changed>` — you added a class in both places;
- `intake: instance-only <why no class should carry it>` — a genuine one-off.

A repair claim is **verified, never trusted**: attest re-classifies with the live
table, and a finding still unmatched under `class-extended` / `new-class` refuses the
attestation until the row lands (or the disposition becomes `instance-only`). A
finding whose repair landed simply matches and owes no intake line. `instance-only`
dispositions are recorded on the sweep attestation (`intake_instance_only`), so the
escape hatch's rate is measurable — an arc that dispositions every unmatched finding
as a one-off has answered the letter of the loop and none of its intent.

**Staleness check:** `scripts/refresh-classes.py` (no verb) re-clusters the live gate
log and prints per-class counts plus recent findings matching NO known class — those
unmatched findings are new-class candidates. Run it when the log has grown
meaningfully since the counts in this file (the class headings are bound to the
corpus named above them).

## Exit condition

The sweep is done when every applicable class has a named answer for the diff — not
"checked, fine" but "class 7: the only env write is X at line N, restored by the
stashed MonkeyPatch at M, sole consumer verified via grep". If the diff introduced a
new data-surface consumer, the inventory table (field × semantics) is part of the
named-answer set — every recorded semantic either tested, or carried as an explicit
deferred finding per the next sentence (sitting in the table is not "named"). Findings you choose not to
fix now must be named in the commit message or register, never silently carried — and
naming is what stops a hold being *silent*, never what makes it *safe*: a held finding
still owes the same-round fail-closed probe (step 4 of the sweep above). Then
commit and invoke the reviewers — they should be confirming, not discovering.

Since B-215, the named-answer set is ATTESTED, not merely written — and since
U-SR-04 (charter WR-10) the labels come BEFORE the answers: after the final
commit, generate the answers file with `HARNESS_ARC_ID=<arc-id>
HARNESS_LANE_ID=<lane-id> just review-template-preflight <answers-file>` — the
destination must live under `.harness/tmp/` (the gitignored scratch namespace; the
verb refuses anywhere else, keeping attestation artifacts out of the stop-gate's
tree-dirty view). It runs
`preflight-grep.sh` over the attested range and writes every hit label into a
fresh template ([B] F14: three attest calls failed by trial only because answers
were authored before the labels existed). Fill every placeholder with the named
answer — attestation refuses a file still carrying one, and a deleted placeholder
is not an answer either: every label section and finding line must carry content
beyond what the template wrote — then attest with the
same-prefixed `just review-attest-preflight <answers-file>`. The inline prefix is
REQUIRED on both verbs exactly as for the review itself (they resolve the arc via
env_arc_and_lane(); a bare invocation binds the branch-* fallback arc, not the
reserved one). The review wrapper refuses round 1 of a reserved arc without a
live attestation (`tools/review_loop_gate.py`; the attestation binds head+diff,
so template + attest after the last commit — the template never overwrites, so
each round's answers file gets a fresh path). The "after every review round"
sweep ends the same way: absorb, commit, `HARNESS_ARC_ID=<arc-id>
HARNESS_LANE_ID=<lane-id> just review-template-sweep <answers-file>` (the prefix
matters here too — a bare invocation queries the branch-* fallback arc's
obligations, finds none, and declines; prefixed, it pre-fills every outstanding
finding_id token-exactly plus the range's hit labels, obligations spanning both
loop channels and all rounds), fill, then the same-prefixed
`just review-attest-sweep <answers-file>`.
