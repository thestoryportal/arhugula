#!/usr/bin/env bash
# Hermetic test for permission-guard.sh (U-HK-12). Synthetic PreToolUse/PermissionRequest
# payloads against a throwaway project dir. Asserts the tri-state: inert off-mode,
# deny-list (force-push, rm -rf, secret reloc, paid MCP), allowlist (safe bash, Read,
# Edit), design-substrate Edit → ask, unknown → ask, and the PermissionRequest schema.

set -uo pipefail
# mutation-probe: tools/hooks/permission-guard.sh:538-540 push-to-main deny predicate stays load-bearing (C-HE-08 §1)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$SCRIPT_DIR/permission-guard.sh"

PASS=0; FAIL=0
ok()  { echo "  ok: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

REPO="$(mktemp -d)"; { [ -n "$REPO" ] && [ -d "$REPO" ]; } || { echo "FATAL mktemp"; exit 1; }
trap 'rm -rf "$REPO"' EXIT
mkdir -p "$REPO/.harness"
# C-HE-09 §2 (U-HE-29): the loop ledger is a SHARED venue outside every worktree, so it is
# no longer reachable as "$REPO/.harness/loop_status.md". Pin it hermetically for this run.
export HARNESS_LOOP_STATUS_PATH="$REPO/shared-loop_status.md"


# Helpers: build a payload + run the hook with loop mode forced via env.
pl() { # $1=tool $2=command $3=file_path $4=event
  jq -nc --arg t "$1" --arg c "$2" --arg f "$3" --arg e "${4:-PreToolUse}" \
    '{"hook_event_name":$e,"tool_name":$t,"tool_input":{"command":$c,"file_path":$f}}'
}
run_on()  { printf '%s' "$1" | HARNESS_LOOP=1 CLAUDE_PROJECT_DIR="$REPO" bash "$HOOK"; }   # loop ON
run_off() { printf '%s' "$1" | CLAUDE_PROJECT_DIR="$REPO" bash "$HOOK"; }                   # loop OFF (unset)
dec()  { echo "$1" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null; }
beh()  { echo "$1" | jq -r '.hookSpecificOutput.decision.behavior // empty' 2>/dev/null; }

unset HARNESS_LOOP

# 1) INERT when loop mode off — even a force-push must produce NO output.
OUT=$(run_off "$(pl Bash 'git push --force origin main' '')")
[ -z "$OUT" ] && ok "inert when loop mode off" || bad "produced output off-mode: $OUT"

# 2) DENY-LIST (loop on).
OUT=$(run_on "$(pl Bash 'git push --force origin main' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "deny force-push" || bad "force-push not denied: $OUT"
OUT=$(run_on "$(pl Bash 'rm -rf build/' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "deny rm -rf" || bad "rm -rf not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git reset --hard origin/main' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "deny git reset --hard" || bad "reset --hard not denied: $OUT"
OUT=$(run_on "$(pl Bash 'cp .env /tmp/backup.env' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "deny secret relocation" || bad "secret reloc not denied: $OUT"
OUT=$(run_on "$(pl Bash 'gh secret set FOO' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "deny gh secret" || bad "gh secret not denied: $OUT"
OUT=$(run_on "$(pl Bash 'curl https://api.anthropic.com/v1/messages' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "deny paid provider call" || bad "provider call not denied: $OUT"
OUT=$(run_on "$(pl mcp__harness-7a-scaffold__route_llm_call '' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "deny paid MCP route_llm_call" || bad "paid MCP not denied: $OUT"

# 3) DENY is logged to the ledger.
grep -q '| DENY |' "$HARNESS_LOOP_STATUS_PATH" && ok "deny logged to ledger" || bad "deny not logged"

# 4) ALLOWLIST.
OUT=$(run_on "$(pl Bash 'git status' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "allow git status" || bad "git status not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git commit -m wip' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "allow git commit" || bad "git commit not allowed: $OUT"

# 4b) Loop-control wrappers (U-HK-14/15) — the P1 regression. The defer-and-continue
#     mechanism's RECORD step must be runnable in loop mode REGARDLESS of args, including a
#     deferral REASON mentioning credentials/secret (the wrapper only appends a ledger row;
#     it bypasses _bash_args_safe). If denied, the headless skip-set never populates and the
#     loop re-attempts the same gated item every iteration. A raw chained `source … &&
#     loop_defer …` (malformed + denied) must still NOT auto-allow.
OUT=$(run_on "$(pl Bash "tools/04-loop/defer.sh R-300 'needs OpenAI credentials — built without it: mock fixture'" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "allow defer.sh wrapper (even with 'credentials' in the reason)" || bad "defer.sh not allowed: $OUT"
OUT=$(run_on "$(pl Bash "bash tools/04-loop/defer.sh R-410 'needs container runtime'" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "allow 'bash tools/04-loop/defer.sh ...'" || bad "bash defer.sh not allowed: $OUT"
OUT=$(run_on "$(pl Bash "tools/04-loop/halt.sh 'forward menu exhausted — 3 awaiting input'" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "allow halt.sh wrapper (stand-down)" || bad "halt.sh not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'source tools/hooks/lib.sh && loop_defer R-1 x' '')")
[ -z "$(dec "$OUT")" ] && ok "chained source+loop_defer NOT auto-allowed (falls to ask — the denied/malformed original)" || bad "chained source auto-allowed: $OUT"
# 4c) Wrapper with an env-var expansion must NOT auto-allow (the shell would expand the
#     secret VALUE into the ledger). Literal "credentials" is fine (4b); `$VAR` is not.
OUT=$(run_on "$(pl Bash 'tools/04-loop/defer.sh R-300 $OPENAI_API_KEY' '')")
[ -z "$(dec "$OUT")" ] && ok "defer.sh with \$VAR expansion NOT auto-allowed (no secret leak)" || bad "defer.sh \$VAR auto-allowed (secret-leak vector): $OUT"
# 4d) A deferral REASON that names an operator action (gh secret / .env) must ALLOW — the
#     wrapper short-circuits BEFORE the free-text deny scan, else the deferral is denied,
#     no ledger row is written, and the headless loop retries the gated item to the cap.
OUT=$(run_on "$(pl Bash "tools/04-loop/defer.sh R-300 'operator must run gh secret set OPENAI_API_KEY'" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "defer.sh reason naming 'gh secret' ALLOWED (deny-scan exempt)" || bad "defer.sh reason tripped deny-list: $OUT"
# 4e) ...but a real `gh secret set` command (not the wrapper) is STILL hard-stopped, and a
#     wrapper with a chained dangerous follow-on is STILL denied (control-op stops the
#     short-circuit → falls through to the deny-list).
OUT=$(run_on "$(pl Bash 'gh secret set FOO' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "real 'gh secret set' still denied (short-circuit is wrapper-only)" || bad "gh secret leaked through: $OUT"
OUT=$(run_on "$(pl Bash 'tools/04-loop/defer.sh R-1 x; rm -rf /' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "wrapper + chained 'rm -rf' still denied (control-op → deny-list)" || bad "chained rm-rf via wrapper not denied: $OUT"
OUT=$(run_on "$(pl Bash 'bash tools/hooks/test_loop_lib.sh' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "allow hermetic test run" || bad "test run not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'gh pr create --fill' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "allow gh pr create" || bad "gh pr not allowed: $OUT"
OUT=$(run_on "$(pl Read '' "$REPO/src/y.py")")
[ "$(dec "$OUT")" = "allow" ] && ok "allow in-worktree Read" || bad "in-worktree Read not allowed: $OUT"
OUT=$(run_on "$(pl Edit '' "$REPO/tools/hooks/foo.sh")")
[ "$(dec "$OUT")" = "allow" ] && ok "allow Edit to normal path" || bad "Edit not allowed: $OUT"

# 5) ASK (no output) — design-substrate Edit + unknown bash + bare git push (non-force).
OUT=$(run_on "$(pl Edit '' '/repo/design-substrate/Spec_X.md')")
[ -z "$OUT" ] && ok "design-substrate Edit → ask (no auto-approve)" || bad "design-substrate auto-decided: $OUT"
OUT=$(run_on "$(pl Bash 'python scripts/migrate.py --wipe' '')")
[ -z "$OUT" ] && ok "unknown bash → ask" || bad "unknown bash auto-decided: $OUT"

# 5b) CHAINED/NESTED command with a safe PREFIX must NOT auto-allow (codex P1). A safe
#     prefix fronting a dangerous follow-on falls through to ask.
OUT=$(run_on "$(pl Bash 'git status && python scripts/migrate.py --wipe' '')")
[ -z "$OUT" ] && ok "safe-prefix && danger → ask (no chained auto-allow)" || bad "chained cmd auto-decided: $OUT"
OUT=$(run_on "$(pl Bash 'uv run python scripts/migrate.py --wipe' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "uv run python wipe → not auto-allowed" || bad "uv-run-python auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash 'cat secrets.txt | sh' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "piped cat | sh → not auto-allowed" || bad "pipe auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git log $(rm -rf /)' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "command substitution → not auto-allowed" || bad "cmd-subst auto-allowed: $OUT"

# 5c) Edit/Write to a secret file must NOT auto-allow (codex P1) — falls through to ask.
OUT=$(run_on "$(pl Edit '' "$REPO/.env")")
[ -z "$OUT" ] && ok ".env Edit → ask (no auto-approve)" || bad ".env edit auto-decided: $OUT"
OUT=$(run_on "$(pl Write '' "$REPO/config/credentials.json")")
[ -z "$OUT" ] && ok "credentials Write → ask" || bad "credentials write auto-decided: $OUT"
OUT=$(run_on "$(pl Write '' '/home/u/.ssh/id_rsa')")
[ -z "$OUT" ] && ok "id_rsa Write → ask" || bad "id_rsa write auto-decided: $OUT"

# 5d) Edit OUTSIDE the worktree / into .git / via traversal must NOT auto-allow (codex P1).
OUT=$(run_on "$(pl Edit '' "$HOME/.claude/settings.json")")
[ -z "$OUT" ] && ok "edit outside worktree → ask" || bad "outside-worktree edit auto-decided: $OUT"
OUT=$(run_on "$(pl Write '' "$REPO/.git/config")")
[ -z "$OUT" ] && ok "edit into .git → ask" || bad ".git edit auto-decided: $OUT"
OUT=$(run_on "$(pl Edit '' "$REPO/../escape.txt")")
[ -z "$OUT" ] && ok "path traversal → ask" || bad "traversal edit auto-decided: $OUT"
OUT=$(run_on "$(pl Edit '' "$REPO/src/mod.py")")
[ "$(dec "$OUT")" = "allow" ] && ok "in-worktree edit → allow" || bad "in-worktree edit not allowed: $OUT"

# 5e) Destructive SUBMODES of allowlisted verbs must NOT auto-allow (codex P1).
OUT=$(run_on "$(pl Bash 'find . -name node_modules -delete' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "find -delete → not auto-allowed" || bad "find -delete auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash 'gh api -X DELETE repos/o/r/issues/1' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "gh api -X DELETE → not auto-allowed" || bad "gh api delete auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash 'uv run python wipe.py' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "uv run python → not auto-allowed" || bad "uv-run-python auto-allowed: $OUT"
# content-reader / programmable-filter verbs are NOT auto-allowed (arg safety unvalidatable)
OUT=$(run_on "$(pl Bash 'find . -name *.py' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "find → ask (content verbs dropped)" || bad "find auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash 'gh api repos/o/r' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "gh api GET → allow" || bad "gh api GET not allowed: $OUT"

# 5f) Workspace-DISCARDING git commands must NOT auto-allow (codex P1) — they can erase
#     uncommitted work and the deny-list only catches reset/rebase/force-push.
for c in "git restore ." "git restore src/mod.py" "git checkout -- ." "git checkout -f main" "git checkout main"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
# but git checkout -b (branch creation, never discards) still auto-allows
OUT=$(run_on "$(pl Bash 'git checkout -b feature/x' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git checkout -b → allow" || bad "git checkout -b not allowed: $OUT"

# 5g) Read tools get the same secret/worktree boundary as edits (codex P1).
OUT=$(run_on "$(pl Read '' "$REPO/.env")")
[ -z "$OUT" ] && ok "Read .env → ask" || bad "Read .env auto-decided: $OUT"
OUT=$(run_on "$(pl Read '' '/etc/passwd')")
[ -z "$OUT" ] && ok "Read outside-worktree → ask" || bad "outside Read auto-decided: $OUT"
OUT=$(run_on "$(jq -nc --arg p "$REPO/src" '{"hook_event_name":"PreToolUse","tool_name":"Grep","tool_input":{"path":$p}}')")
[ "$(dec "$OUT")" = "allow" ] && ok "Grep in-worktree → allow" || bad "in-worktree Grep not allowed: $OUT"
OUT=$(run_on "$(jq -nc '{"hook_event_name":"PreToolUse","tool_name":"Grep","tool_input":{"path":"/etc"}}')")
[ -z "$OUT" ] && ok "Grep outside-worktree → ask" || bad "outside Grep auto-decided: $OUT"

# 5h) gh: only enumerated read/safe subcommands auto-allow; mutating ones fall to ask (codex P1).
# C-HE-07: raw merge verb DENIED in loop mode; only the safe-merge wrapper is auto-allowed.
OUT=$(run_on "$(pl Bash 'gh pr merge 268 --squash --delete-branch' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "raw gh pr merge → deny (loop mode)" || bad "raw merge not denied: $OUT"
OUT=$(run_on "$(pl Bash 'bash tools/hooks/safe-merge.sh 268' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "safe-merge wrapper → allow" || bad "wrapper not allowed: $OUT"
for c in 'bash tools/hooks/safe-merge.sh 268 --squash' 'bash tools/hooks/safe-merge.sh $PR' 'bash tools/hooks/safe-merge.sh 268; rm x' 'bash tools/hooks/safe-merge.sh abc' 'tools/hooks/safe-merge.sh'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "wrapper hardening: '$c' → not allow" || bad "wrapper over-matched: $c"
done
# U-HE-37 pilot verbs: arity-bounded to ONE bareword run id. A surplus token is parsed by
# `just` as a chained SECOND recipe, so the not-end-anchored generic verb alternation would
# auto-approve `just lanes-pilot p1 <any-ask-gated-recipe>` (codex r1 P1 on u-he-37).
for c in 'just lanes-pilot pilot-1 main-protection-rollback' \
         'just lanes-pilot-report pilot-1 main-protection-apply' \
         'just lanes-pilot' \
         'just lanes-pilot-report' \
         'just lanes-pilot $RUN' \
         'just lanes-pilot pilot-1; rm x' \
         'just lanes-pilot pilot-1 && just main-protection-rollback' \
         'just lanes-pilot my-secret-run' \
         'just lanes-pilot-report ..parent'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "pilot arity: '$c' → not allow" || bad "pilot verb over-matched: $c"
done

# direct-exec form: the C-HE-07 §1 verbatim matcher makes the `bash` token optional
# (mirrors _safe_worktree_remove_wrapper) — pinned so the shape is deliberate, not drift.
OUT=$(run_on "$(pl Bash 'tools/hooks/safe-merge.sh 268' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "direct-exec wrapper form → allow (verbatim matcher)" || bad "direct-exec wrapper not allowed: $OUT"
# wrapper with the exact-shape HARNESS_* bareword prefix (codex r1 P1: safe-merge.sh
# requires both ids; exports do not survive across Bash tool calls)
OUT=$(run_on "$(pl Bash 'HARNESS_ARC_ID=u-he-25 HARNESS_LANE_ID=lane-1 bash tools/hooks/safe-merge.sh 268' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "HARNESS_* prefixed wrapper → allow" || bad "prefixed wrapper not allowed: $OUT"
for c in 'HARNESS_FAILOVER_CHILD=1 bash tools/hooks/safe-merge.sh 268' \
         'HARNESS_ARC_ID=$ARC bash tools/hooks/safe-merge.sh 268' \
         'HARNESS_ARC_ID="u he" bash tools/hooks/safe-merge.sh 268'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "prefixed-wrapper hardening: '$c' → not allow" || bad "prefixed wrapper over-matched: $c"
done
# DENY-row audit of the raw-merge denial flows through emit_deny → loop_log DENY; its
# observable venue (loop_status.md row) lands with U-HE-29 — no assertion here until then
# (codex r2 P3: a `grep || true` pseudo-assertion would be unconditionally green).

# C-HE-07 hermetic EXECUTION witness for safe-merge.sh (codex r3 P2): the wrapper itself is
# run — arity/digit validation, env preconditions abort BEFORE delegation, and the exact
# merge_door.py land delegation (arg-boundary-exact via a stubbed `uv` on PATH; the
# --refresh-cmd value must arrive as ONE argument). No real merge/door/gh is reachable.
SMWRAP="$SCRIPT_DIR/safe-merge.sh"
SMDIR="$REPO/sm"; mkdir -p "$SMDIR/bin"; ( cd "$SMDIR" && git init -q . )
# Stub uv: answers the wrapper's pre-lease `--help` availability probe (default: flag
# present; override via SM_HELP_OUT), records any other invocation's argv one-per-line.
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'case "$*" in'
  printf '%s\n' '  *--help*) printf "%s\n" "${SM_HELP_OUT:---emit-refresh-pr-json}" ;;'
  printf '%s\n' '  *) printf "%s\n" "$@" > "$SM_ARGS_OUT" ;;'
  printf '%s\n' 'esac'
} > "$SMDIR/bin/uv"
chmod +x "$SMDIR/bin/uv"
( cd "$SMDIR" && bash "$SMWRAP" ) >/dev/null 2>&1
[ $? -eq 64 ] && ok "safe-merge: no arg → exit 64" || bad "safe-merge no-arg rc wrong"
( cd "$SMDIR" && bash "$SMWRAP" abc ) >/dev/null 2>&1
[ $? -eq 64 ] && ok "safe-merge: non-digit pr → exit 64" || bad "safe-merge non-digit rc wrong"
( cd "$SMDIR" && bash "$SMWRAP" 268 --squash ) >/dev/null 2>&1
[ $? -eq 64 ] && ok "safe-merge: extra flag → exit 64" || bad "safe-merge extra-flag rc wrong"
( cd "$SMDIR" && SM_ARGS_OUT="$SMDIR/args" PATH="$SMDIR/bin:$PATH" \
  env -u HARNESS_LANE_ID HARNESS_ARC_ID=u-he-25 bash "$SMWRAP" 268 ) >/dev/null 2>&1
if [ $? -ne 0 ] && [ ! -f "$SMDIR/args" ]; then
  ok "safe-merge: missing HARNESS_LANE_ID aborts pre-delegation"
else
  bad "safe-merge ran without lane id (args file: $(cat "$SMDIR/args" 2>/dev/null))"
fi
( cd "$SMDIR" && SM_ARGS_OUT="$SMDIR/args" PATH="$SMDIR/bin:$PATH" \
  HARNESS_LANE_ID=lane-1 HARNESS_ARC_ID=u-he-25 bash "$SMWRAP" 268 ) >/dev/null 2>&1
SM_EXPECT='run
python
tools/merge_door.py
land
268
--lane-id
lane-1
--arc-id
u-he-25
--refresh-cmd
uv run python tools/roadmap_status_refresh.py --emit-refresh-pr-json 268'
if [ "$(cat "$SMDIR/args" 2>/dev/null)" = "$SM_EXPECT" ]; then
  ok "safe-merge: delegates the exact merge_door land invocation"
else
  bad "safe-merge delegation drifted: $(cat "$SMDIR/args" 2>/dev/null | tr '\n' ' ')"
fi
# Pre-lease availability guard (merge-gate r1): flag ABSENT from the probed CLI → the
# wrapper aborts exit 69 BEFORE any delegation (no lease, no merge — the deterministic
# post-merge door-wedge the three lenses traced cannot start).
rm -f "$SMDIR/args"
( cd "$SMDIR" && SM_ARGS_OUT="$SMDIR/args" SM_HELP_OUT='no such flag here' PATH="$SMDIR/bin:$PATH" \
  HARNESS_LANE_ID=lane-1 HARNESS_ARC_ID=u-he-25 bash "$SMWRAP" 268 ) >/dev/null 2>&1
if [ $? -eq 69 ] && [ ! -f "$SMDIR/args" ]; then
  ok "safe-merge: unsupported refresh flag aborts pre-lease (exit 69, no delegation)"
else
  bad "safe-merge ran despite unsupported refresh flag (args: $(cat "$SMDIR/args" 2>/dev/null))"
fi
# REAL-CLI posture pin (flipped by U-HE-28 per the U-HE-25 rev note): the real
# roadmap_status_refresh.py now supports --emit-refresh-pr-json, so the wrapper's
# pre-lease availability probe PASSES against the REAL repo + REAL uv and delegation
# proceeds — where the REAL merge_door refuses the nonexistent witness reservation
# fail-fast (DoorFailed "no reservation" → rc 4) BEFORE any lease is taken or merge
# attempted. rc 69 here means the flag regressed out of the CLI; rc 0/3/5 would mean
# the door somehow engaged real state for a witness arc — both are defects.
REALROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
( cd "$REALROOT" && HARNESS_LANE_ID=lane-witness HARNESS_ARC_ID=arc-witness bash "$SMWRAP" 999999 ) >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 4 ]; then
  ok "safe-merge: REAL CLI probe passes; door refuses the witness arc fail-fast (rc 4)"
elif [ "$RC" -eq 69 ]; then
  bad "safe-merge real-CLI pin: pre-lease abort 69 — --emit-refresh-pr-json regressed out of roadmap_status_refresh.py"
elif [ "$RC" -eq 64 ]; then
  bad "safe-merge real-CLI pin hit arity path unexpectedly"
else
  bad "safe-merge real-CLI pin: expected door fail-fast rc 4, got rc=$RC"
fi
OUT=$(run_on "$(pl Bash 'gh run view 5' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "gh run view → allow" || bad "gh run view not allowed: $OUT"
for c in "gh pr close 123 --delete-branch" "gh run cancel 5" "gh api repos/o/r --raw-field x=y" "gh pr edit 1 --title z"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done

# 5i) bash tools/ is restricted to test_*.sh entrypoints (codex P2).
OUT=$(run_on "$(pl Bash 'bash tools/hooks/test_lib.sh' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "bash tools test_*.sh → allow" || bad "test script not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'bash tools/04-loop/run.sh' '')")
[ -z "$OUT" ] && ok "bash tools non-test script → ask" || bad "arbitrary tools script auto-decided: $OUT"

# 5j) Bash allowlist verbs must not auto-allow secret/home/outside ARGS (codex P1).
for c in "cat .env" "grep TOKEN ~/.ssh/id_rsa" "touch ~/.ssh/authorized_keys" "cat /etc/passwd" "head ../outside.txt"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
OUT=$(run_on "$(pl Bash 'cat README.md' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "cat → ask (content verbs dropped; use Read tool)" || bad "cat auto-allowed: $OUT"

# 5k) git commit --amend (history rewrite) → ask (codex P2).
OUT=$(run_on "$(pl Bash 'git commit --amend --no-edit' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "git commit --amend → not auto-allowed" || bad "amend auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git commit -m wip' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "plain git commit → still allow" || bad "plain commit not allowed: $OUT"

# 5l) Notebook tools use notebook_path, not file_path (codex P2).
OUT=$(run_on "$(jq -nc --arg p "$REPO/nb.ipynb" '{"hook_event_name":"PreToolUse","tool_name":"NotebookEdit","tool_input":{"notebook_path":$p}}')")
[ "$(dec "$OUT")" = "allow" ] && ok "in-worktree NotebookEdit → allow" || bad "in-worktree notebook not allowed: $OUT"
OUT=$(run_on "$(jq -nc '{"hook_event_name":"PreToolUse","tool_name":"NotebookEdit","tool_input":{"notebook_path":"/etc/x.ipynb"}}')")
[ -z "$OUT" ] && ok "outside NotebookEdit → ask" || bad "outside notebook auto-decided: $OUT"

# 5m) Round-6 adversarial bypasses must NOT auto-allow (codex P1/P2).
for c in "cat '/etc/passwd'" "echo \$ANTHROPIC_API_KEY" "awk 'BEGIN{system(\"git push origin main\")}'" "git branch --delete --force x" "printf %s \$OPENAI_API_KEY"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
# git branch (list/create) still allowed
OUT=$(run_on "$(pl Bash 'git branch' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git branch (list) → allow" || bad "git branch list not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git branch -d merged-feature' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git branch -d → allow safe merged-branch cleanup" || bad "safe branch cleanup not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git push origin feature' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git push → allow normal arc publication" || bad "normal push not allowed: $OUT"
for c in "git push --mirror" "git push --prune origin" "git worktree add -B existing /tmp/new-arc HEAD"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "deny" ] && ok "'$c' → deny destructive mutation" || bad "'$c' not denied: $OUT"
done
for c in \
  "git fetch --upload-pack=/tmp/attacker origin" \
  "git fetch -u /tmp/attacker origin" \
  "git fetch origin --upload-pack=/tmp/attacker"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] \
    && ok "'$c' → not auto-allowed (execution-bearing fetch option)" \
    || bad "'$c' auto-allowed: $OUT"
done
OUT=$(run_on "$(pl Bash 'git ls-remote --heads origin refs/heads/topic' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git ls-remote → allow read-only branch hygiene probe" || bad "ls-remote not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git pull --ff-only' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git pull --ff-only → allow main sync" || bad "ff-only pull not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git merge --no-edit main' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git merge --no-edit main → allow topic base sync" || bad "main merge not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git merge --no-edit origin/main' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git merge --no-edit origin/main → allow fetched base sync" || bad "origin/main merge not allowed: $OUT"
for c in "git merge --abort" "git merge --strategy=ours main" "git merge --no-edit feature/unreviewed"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
OUT=$(run_on "$(pl Bash 'git worktree remove /tmp/merged-clean' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "direct git worktree remove → deny (must use mutex wrapper)" || bad "direct worktree removal not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git -C /tmp/repo worktree remove /tmp/merged-clean' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "git -C direct worktree remove → deny" || bad "git -C worktree removal not denied: $OUT"
OUT=$(run_on "$(pl Bash 'tools/hooks/safe-worktree-remove.sh /tmp/merged-clean' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "mutex-backed worktree remove wrapper → allow" || bad "safe worktree wrapper not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'tools/hooks/safe-worktree-remove.sh "/tmp/merged-clean"' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "quoted mutex-backed worktree path → allow" || bad "quoted safe worktree path not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git worktree add /tmp/new-arc feature' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git worktree add → allow non-force arc creation" || bad "non-force worktree creation not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'git worktree add -b feature /tmp/new-arc' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git worktree add -b → allow explicit /tmp arc creation" || bad "-b worktree creation not allowed: $OUT"
for c in \
  "git worktree add /tmp/new-arc feature; touch /tmp/escaped" \
  "git worktree add /tmp/new-arc feature && touch /tmp/escaped" \
  "git worktree remove /tmp/merged-clean | sh" \
  'git worktree add /tmp/$(touch-escaped) feature'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
for c in "git push origin --delete feature" "git worktree remove --force /tmp/dirty" "git worktree add --force /tmp/rebind feature"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "deny" ] && ok "'$c' → deny destructive cleanup" || bad "'$c' not denied: $OUT"
done
for c in "git worktree add \$HOME/escape feature" "git worktree add /etc/escape feature" "git worktree remove /etc/registered"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed outside bounded roots" || bad "'$c' auto-allowed: $OUT"
done

# 5n.1) The controller's own provider-free lifecycle and fresh read-only merge lenses
# must not stop a headless loop for approval. Keep the allowlist recipe-specific and
# require the nested Codex process to be ephemeral + read-only.
for c in \
  "just codex-preflight" \
  "just codex-checkpoint after-review" \
  "just codex-closeout" \
  "just codex-context-check" \
  "just codex-context-check-ci" \
  "just codex-autonomous-arc R-123" \
  "just codex-loop-record --phase plan --status passed --command plan --evidence grounded" \
  "just codex-loop-status" \
  "just codex-loop-check" \
  "just codex-worktree-gc" \
  "just codex-worktree-gc --reap" \
  "just review-with-failover" \
  "just review-with-failover main" \
  "just review-attest-preflight .harness/tmp/preflight-answers.md" \
  "just review-attest-sweep .harness/tmp/sweep-answers.md main" \
  "just review-template-preflight .harness/tmp/preflight-answers.md" \
  "just review-template-sweep .harness/tmp/sweep-answers.md main" \
  "just review-gate-check" \
  "just merge-gate-binding merge-gate-concurrency" \
  "just merge-gate-binding merge-gate-spec-conformance main" \
  "just merge-gate-emit --pr 1397 --lens merge-gate-concurrency --verdict-json .harness/tmp/merge-gate-lens-concurrency.txt --base main" \
  "just merge-gate-emit-all --pr 1397 --arc-id b-230-task-5 --concurrency-json .harness/tmp/merge-gate-lens-concurrency.txt --spec-json .harness/tmp/merge-gate-lens-spec-conformance.txt --witness-json .harness/tmp/merge-gate-lens-witness-adequacy.txt" \
  "just merge-gate-log-check" \
  "just merge-gate-landing-delta 0123456789abcdef0123456789abcdef01234567" \
  "just lanes-verify" \
  "just lanes-phase0-check" \
  "just lanes-pilot pilot-1" \
  "just lanes-pilot-report pilot-1" \
  "just mutation-probe-coverage-check" \
  "just reviewer-concurrency-probe" \
  "just reviewer-concurrency-probe gemini 5 main" \
  "just overlay-check" \
  "just arc-close 1503 b897542dc .harness/.checkpoints/arc-exit-report-pr1503.md --arc-id b-230-task-1 --arc-type applying --decisions 0 --round-logs .harness/tmp/b-230-task-1-rounds/r1.log .harness/tmp/b-230-task-1-rounds/r2.log --levers B-211 B-212"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "allow" ] && ok "'$c' → allow controller lifecycle" || bad "'$c' not allowed: $OUT"
done
# B-230 Task 3: arc-close rides the generic just alternation; the token grammar and
# _bash_args_safe keep the glob and out-of-worktree transcript forms at ask (B-217 owns
# any widening) — pinned here so a charset change is a visible decision, not drift.
OUT=$(run_on "$(pl Bash "just arc-close 1503 b897542dc cp.md --arc-id b-230-task-1 --arc-type applying --decisions 0 --round-logs '.harness/tmp/b-230-task-1-rounds/*.log'" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "arc-close with a glob --round-logs → ask (token grammar)" || bad "glob arc-close auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just arc-close 1503 b897542dc cp.md --arc-id b-230-task-1 --arc-type applying --decisions 0 --round-logs r1.log --transcript ~/.claude/projects/x/s.jsonl" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "arc-close with a ~ transcript → ask" || bad "tilde arc-close auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just arc-close 1503 b897542dc cp.md --arc-id b-230-task-1 --arc-type applying --decisions 0 --round-logs r1.log --transcript /Users/someone/.claude/projects/x/s.jsonl" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "arc-close with an out-of-worktree transcript → ask" || bad "out-of-worktree arc-close auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just merge-gate-emit --pr 1 --lens merge-gate-concurrency --verdict-json /tmp/outside.txt" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "merge-gate-emit reading a verdict file outside the worktree → not auto-allowed" || bad "out-of-worktree merge-gate-emit auto-allowed: $OUT"
# B-230 Task 5: emit-all rides the same alternation; one out-of-worktree verdict path
# among the three drops the whole call to ask, exactly as the single-lens form does.
OUT=$(run_on "$(pl Bash "just merge-gate-emit-all --pr 1 --arc-id b-230-task-5 --concurrency-json .harness/tmp/c.txt --spec-json /tmp/outside.txt --witness-json .harness/tmp/w.txt" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "merge-gate-emit-all with one verdict file outside the worktree → not auto-allowed" || bad "out-of-worktree merge-gate-emit-all auto-allowed: $OUT"
# U-HE-47 codex r2 P1 + r4 P1: adjudication mutes findings — only the exact absorption
# shape (accepted|rejected, an absorber-identity actor, a review-producer 4-part
# finding-id, bounded arity) AND the HARNESS_ARC_ID= prefix (the CLI's arc-binding
# input) auto-allow; everything else stays operator-visible.
_ADJ_FID="merge-gate-concurrency:0123456789abcdef0123456789abcdef01234567:0123456789ab:1"
OUT=$(run_on "$(pl Bash "HARNESS_ARC_ID=u-he-47 just merge-gate-adjudicate --finding-id $_ADJ_FID --disposition accepted --actor claude_absorber" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "prefixed exact-shape adjudicate → allow" || bad "prefixed exact-shape adjudicate not allowed: $OUT"
OUT=$(run_on "$(pl Bash "just merge-gate-adjudicate --finding-id $_ADJ_FID --disposition accepted --actor claude_absorber" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "UNPREFIXED adjudicate (arc-unbound) → not auto-allowed" || bad "arc-unbound adjudication auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "HARNESS_ARC_ID=u-he-47 just merge-gate-adjudicate --finding-id $_ADJ_FID --disposition accepted --actor third_party_reviewer" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "adjudicate with a non-absorber actor → not auto-allowed" || bad "non-absorber actor auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just merge-gate-adjudicate --finding-id $_ADJ_FID --disposition suppressed --actor claude_absorber" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "adjudicate --disposition suppressed → not auto-allowed (operator-visible)" || bad "suppressed adjudication auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just merge-gate-adjudicate --finding-id $_ADJ_FID --disposition accepted --actor operator" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "adjudicate claiming actor=operator → not auto-allowed" || bad "operator-actor adjudication auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just merge-gate-adjudicate --finding-id $_ADJ_FID --disposition=accepted --actor claude_absorber" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "adjudicate =-form disposition → not auto-allowed (exact tokens only)" || bad "=-form adjudication auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just merge-gate-adjudicate --finding-id $_ADJ_FID --disposition accepted --actor claude_absorber review-attest-budget" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "adjudicate with a surplus chained token → not auto-allowed" || bad "chained-token adjudication auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just merge-gate-adjudicate --finding-id not-a-4-part-id --disposition accepted --actor claude_absorber" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "adjudicate with a malformed finding-id → not auto-allowed" || bad "malformed-id adjudication auto-allowed: $OUT"
# codex r3 P1: a deterministic detection's finding (non-review producer) must not be
# adjudicable headlessly — rejecting it would mute the detection downstream.
OUT=$(run_on "$(pl Bash "just merge-gate-adjudicate --finding-id merge-door-lease-acquire:0123456789abcdef0123456789abcdef01234567:0123456789ab:1 --disposition rejected --actor claude_absorber" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "adjudicate targeting a deterministic-check producer → not auto-allowed" || bad "deterministic-producer adjudication auto-allowed: $OUT"
# r7 P2 (settles the r4→r6→r7 oscillation): the guard serves BOTH runners
# (.codex/hooks.json registers this same script), so both documented absorber
# identities allow; the cross-runner WHO claim is a registered auditable residual.
OUT=$(run_on "$(pl Bash "HARNESS_ARC_ID=u-he-47 just merge-gate-adjudicate --finding-id codex_review_wrapper:0123456789abcdef0123456789abcdef01234567:0123456789ab:1 --disposition accepted --actor claude_absorber" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "adjudicate as claude_absorber → allow" || bad "claude_absorber adjudication not allowed: $OUT"
OUT=$(run_on "$(pl Bash "HARNESS_ARC_ID=u-he-47 just merge-gate-adjudicate --finding-id codex_review_wrapper:0123456789abcdef0123456789abcdef01234567:0123456789ab:1 --disposition accepted --actor codex_absorber" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "adjudicate as codex_absorber → allow (shared-runner guard)" || bad "codex_absorber adjudication not allowed: $OUT"
# B-215: the budget-extension verb is deliberately ask-gated — the loop must never
# silently extend its own review budget; and an out-of-worktree answers path fails
# _bash_args_safe containment even on an allowlisted attest verb.
OUT=$(run_on "$(pl Bash "just review-attest-budget 2 operator-approved" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "review-attest-budget → not auto-allowed (operator-visible)" || bad "review-attest-budget auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just review-attest-preflight /tmp/outside-answers.md" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "review-attest-preflight with out-of-worktree answers → not auto-allowed" || bad "out-of-worktree attest answers auto-allowed: $OUT"
# codex r3 P1: `just` chains recipes — a prefix-anchored allow would let the
# ask-gated budget verb ride as a chained SECOND recipe after an allowlisted one.
# The exact-anchored shapes bound arity so no surplus token can name a recipe.
OUT=$(run_on "$(pl Bash "just review-gate-check main review-attest-budget 2 operator-approved" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "chained review-attest-budget after review-gate-check → not auto-allowed" || bad "multi-recipe budget chain auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just review-attest-sweep .harness/tmp/a.md main review-attest-budget 2 ok" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "chained budget after review-attest-sweep → not auto-allowed" || bad "attest-sweep budget chain auto-allowed: $OUT"
# U-SR-04: the template verbs share the attest shapes' arity bound and containment —
# same negative surface: no out-of-worktree destination, no chained second recipe.
OUT=$(run_on "$(pl Bash "just review-template-preflight /tmp/outside-answers.md" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "review-template-preflight with out-of-worktree answers → not auto-allowed" || bad "out-of-worktree template answers auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just review-template-sweep .harness/tmp/a.md main review-attest-budget 2 ok" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "chained budget after review-template-sweep → not auto-allowed" || bad "template-sweep budget chain auto-allowed: $OUT"
# codex r5 P1: the budget verb chained after ANY generic-prefix allowlisted recipe
# (the B-217 class) — the load-bearing ask-gate is pinned by a pre-allowlist reject
# on every `just` line mentioning it, regardless of which prefix fronts the chain.
OUT=$(run_on "$(pl Bash "just codex-loop-status review-attest-budget 2 ok" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "chained budget after generic-prefix recipe → not auto-allowed" || bad "generic-prefix budget chain auto-allowed: $OUT"
# codex r6 P1: shell quote-normalization (review-attest-"budget") defeats the raw
# grep — any just-first command containing a quote character falls through to ask.
OUT=$(run_on "$(pl Bash 'just codex-loop-status review-attest-"budget" 2 ok' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "quote-normalized budget chain → not auto-allowed" || bad "quote-normalized budget chain auto-allowed: $OUT"
# U-HE-35: the probe allow is arity-bounded to its 3 recipe parameters — a 4th token
# would be parsed by `just` as a chained second recipe and must fall through to ask.
OUT=$(run_on "$(pl Bash "just reviewer-concurrency-probe codex 5 main review-attest-budget 2 ok" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "chained recipe after reviewer-concurrency-probe → not auto-allowed" || bad "probe chain auto-allowed: $OUT"
# U-HE-35 codex r3/r4: reps is TYPED and bounded to a single digit (order-of-contract;
# the C-HE-22 bar is 5) — 99 reps = ~700 live calls outside any arc round budget, and an
# unbounded count would spend billions; anything past 9 stays operator-visible.
OUT=$(run_on "$(pl Bash "just reviewer-concurrency-probe codex 1000000000 main" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "unbounded probe reps → not auto-allowed" || bad "unbounded probe reps auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just reviewer-concurrency-probe codex 99 main" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "two-digit probe reps → not auto-allowed" || bad "two-digit probe reps auto-allowed: $OUT"
# codex r7: reps below the C-HE-22 bar of 5 are guaranteed RED-insufficient yet would
# still spawn live budget-exempt calls — not auto-allowed either.
OUT=$(run_on "$(pl Bash "just reviewer-concurrency-probe codex 1 main" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "below-contract probe reps → not auto-allowed" || bad "below-contract probe reps auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just reviewer-concurrency-probe evilchannel 5 main" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "unknown probe channel → not auto-allowed" || bad "unknown probe channel auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash "just codex-loop-status review-attest-bud'get' 2 ok" '')")
[ "$(dec "$OUT")" != "allow" ] && ok "single-quote-normalized budget chain → not auto-allowed" || bad "single-quote budget chain auto-allowed: $OUT"
# codex r8 P1: the token grammar admits FULLY-quoted plain spans — the documented
# quoted-evidence loop-record shape must stay allowed (r7's bare charset broke it).
OUT=$(run_on "$(pl Bash 'just codex-loop-record --phase plan --status passed --command plan --evidence "expected assertion"' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "quoted-evidence codex-loop-record → allow" || bad "quoted-evidence loop-record not allowed: $OUT"
# codex r10 P1: real evidence prose carries parens — "2 passed (0.1s)" must allow.
OUT=$(run_on "$(pl Bash 'just codex-loop-record --phase red --status passed --command pytest --evidence "2 passed (0.1s)"' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "paren-evidence codex-loop-record → allow" || bad "paren-evidence loop-record not allowed: $OUT"
# codex r7 P1: backslash normalization (review-attest-bud\get) — the positive
# plain-charset constraint on just-first commands ends the whole normalization
# ladder (quotes, backslashes, $VAR splices, globs) in one rule.
OUT=$(run_on "$(pl Bash 'just check review-attest-bud\get 2 ok' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "backslash-normalized budget chain → not auto-allowed" || bad "backslash budget chain auto-allowed: $OUT"
OUT=$(run_on "$(pl Bash 'just check review-attest-bud[g]et 2 ok' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "glob-normalized budget chain → not auto-allowed" || bad "glob budget chain auto-allowed: $OUT"
SAFE_CODEX_CMD="env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'read lens1 prompt"$'\n'"whose reviewed text uses ; and workspace-write sandbox_mode -s'"
OUT=$(run_on "$(pl Bash "$SAFE_CODEX_CMD" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "fresh read-only codex exec → allow merge lens" || bad "read-only codex exec not allowed: $OUT"
OUT=$(run_on "$(pl Bash "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens2-0123456789abcdef0123456789abcdef01234567.md --ephemeral --sandbox read-only -- 'read lens2 prompt'" '')")
[ "$(dec "$OUT")" = "allow" ] && ok "fresh read-only codex exec → allow merge lens with -C first" || bad "-C-first read-only codex exec not allowed: $OUT"
for c in \
  "codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env OTHER=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox workspace-write -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox danger-full-access -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C /repo --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /etc/review.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens3-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'; touch /tmp/escaped" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-6-astra\" -c model_reasoning_effort=\"medium\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"high\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" -c model=\"gpt-5.6-sol\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "env HARNESS_CODEX_REVIEW_ISOLATED=1 codex exec -c model=\"gpt-5.6-sol\" -c model_reasoning_effort=\"medium\" -c sandbox_mode=\"danger-full-access\" --ephemeral --sandbox read-only -C $REPO --output-last-message /tmp/arhugula-pr-1186-lens1-0123456789abcdef0123456789abcdef01234567.md -- 'inspect'" \
  "just codex-autonomous-arc R-1; git push --force"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done

# 5n) Round-7: braced/any uppercase env expansion + relative design-substrate (codex P1).
for c in "cat \${HOME}/.claude/settings.json" "mkdir \${HOME}/tmp" "cat \$TMPDIR/x"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
OUT=$(run_on "$(pl Edit '' 'design-substrate/Spec_X.md')")
[ -z "$OUT" ] && ok "relative design-substrate Edit → ask" || bad "relative design-substrate auto-decided: $OUT"

# 5o) Round-8: .git internals, admin merge, symlink escape (codex P1/P2).
for c in "cat .git/config" "touch .git/hooks/pre-commit" "chmod +x .git/hooks/pre-commit" "gh pr merge 1 --admin"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
# .github (not .git) is not mistaken for git internals (use a kept verb: git diff)
OUT=$(run_on "$(pl Bash 'git diff .github/workflows/ci.yml' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git diff .github/... → allow (not .git)" || bad ".github path rejected as .git: $OUT"
OUT=$(run_on "$(pl Bash 'gh pr merge 1 --squash' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "raw gh pr merge --squash → deny" || bad "raw merge --squash not denied: $OUT"
OUT=$(run_on "$(pl Bash 'gh pr merge 5 --admin' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "--admin merge stays denied" || bad "--admin merge not denied: $OUT"

# U-HE-25 registered allowlist additions (from U-HE-21 codex r1-r6; EXACT-SHAPE only).
# (a) reservations.py carrier verbs — selectable|show|reserve|update|mint-lane-id ONLY.
for c in 'uv run python tools/reservations.py selectable --arc-id u-he-25' \
         'uv run python tools/reservations.py show --arc-id u-he-25' \
         'uv run python tools/reservations.py reserve --arc-id u-he-25 --lane-id lane-1 --branch feat/x --arc-type applying' \
         'uv run python tools/reservations.py update --arc-id u-he-25 --pr 1' \
         'uv run python tools/reservations.py mint-lane-id'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "allow" ] && ok "reservations carrier verb → allow: '$c'" || bad "reservations carrier not allowed: $c → $OUT"
done
# (a'''') U-HE-34: the `phase` span emitter — POSITIONAL exact shape only (codex r1 P1:
# argparse is last-value-wins and record_phase has no holder check, so a duplicated
# --arc-id riding a prefix allowlist would write onto another lane's reservation).
OUT=$(run_on "$(pl Bash 'uv run python tools/reservations.py phase --arc-id u-he-34 --phase execute --edge start --lane-id lane-1' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "phase exact shape → allow" || bad "phase exact shape not allowed → $OUT"
for c in 'uv run python tools/reservations.py phase --arc-id a --phase execute --edge start --lane-id l --arc-id victim' \
         'uv run python tools/reservations.py phase --arc-id a --phase execute --edge start' \
         'uv run python tools/reservations.py phase --arc-id=a --phase execute --edge start --lane-id l' \
         'uv run python tools/reservations.py phase --arc-id a --phase execute --edge start --lane-id l --ts 2026-01-01T00:00:00Z' \
         'uv run python tools/reservations.py phase --arc a --phase execute --edge start --lane-id l' \
         'uv run python tools/reservations.py phase --arc-id a --edge start --phase execute --lane-id l' \
         'uv run python tools/reservations.py phase --arc-id a --phase execute --edge nonsense --lane-id l' \
         'uv run python tools/reservations.py phase --arc-id ${victim:=other} --phase execute --edge start --lane-id l' \
         'uv run python tools/reservations.py phase --arc-id a --phase execute --edge start --lane-id $victim'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "phase hardening: '$c' → not allow" || bad "phase over-matched: $c"
done
# (a''''') U-HE-34 r4: the settle recipe was REMOVED (a synchronous in-recipe tee
# cannot observe the reviewer-internal log-write divergence; B-218 carries the
# wrapper-internal recorder) — its former allow shape must no longer match anything.
OUT=$(run_on "$(pl Bash 'just review-log-settle .harness/tmp/r1.log' '')")
[ "$(dec "$OUT")" != "allow" ] && ok "removed review-log-settle → not allow" || bad "review-log-settle still allowlisted"
# (a'''''') U-HE-34 r3: the logged review variant — tee inside the recipe; bare and
# HARNESS-prefixed forms allow, chained/expansion forms fall through.
for c in 'just review-with-failover-logged .harness/tmp/u-he-34-rounds/r1.log' \
         'just review-with-failover-logged .harness/tmp/u-he-34-rounds/r1.log main' \
         'HARNESS_ARC_ID=u-he-34 HARNESS_LANE_ID=lane-1 just review-with-failover-logged .harness/tmp/u-he-34-rounds/r1.log'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "allow" ] && ok "review-logged → allow: '$c'" || bad "review-logged not allowed: $c → $OUT"
done
for c in 'just review-with-failover-logged .harness/tmp/r1.log main review-attest-budget' \
         'just review-with-failover-logged ${victim:=x}.log' \
         'just review-with-failover-logged -a' \
         'just review-with-failover-logged .harness/tmp/r1.log -b' \
         'just review-with-failover-logged tools/reservations.py' \
         'just review-with-failover-logged .harness/merge-gate-log.jsonl' \
         'just review-with-failover-logged .harness/tmp/../../etc/x.log' \
         'just review-with-failover-logged /etc/x.log' \
         'just review-with-failover-logged'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "review-logged hardening: '$c' → not allow" || bad "review-logged over-matched: $c"
done
# hardening: state-mutating / gh-backed / non-carrier verbs and the bare module prefix stay un-allowed
for c in 'uv run python tools/reservations.py transition --arc-id x --to merged' \
         'uv run python tools/reservations.py gc' \
         'uv run python tools/reservations.py reconcile-all' \
         'uv run python tools/reservations.py' \
         'uv run python tools/other.py selectable'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "reservations hardening: '$c' → not allow" || bad "reservations over-matched: $c"
done
# (a'') U-HE-36: the selection-time disjointness gate (C-HE-13 §5) — the read-only
# `check` verb only; `derive-pairs` (writes a tracked file via gh) and `historical`
# (a minutes-long replay) stay operator recipes, and expansions fall to _bash_args_safe.
for c in 'uv run python tools/arc_disjoint_check.py check' \
         'uv run python tools/arc_disjoint_check.py check --candidate HEAD' \
         'uv run python tools/arc_disjoint_check.py check --candidate feat/u-he-36-arc-disjoint-check' \
         'uv run python tools/arc_disjoint_check.py check --candidate HEAD^'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "allow" ] && ok "arc_disjoint_check check → allow: '$c'" || bad "arc_disjoint_check check not allowed: $c → $OUT"
done
for c in 'uv run python tools/arc_disjoint_check.py derive-pairs' \
         'uv run python tools/arc_disjoint_check.py historical' \
         'uv run python tools/arc_disjoint_check.py' \
         'uv run python tools/arc_disjoint_check.py check --candidate $(git rev-parse HEAD)' \
         'uv run python tools/arc_disjoint_check.py check --candidate ${victim:=x}' \
         'uv run python tools/arc_disjoint_check.py check --candidate HEAD --candidate x' \
         'uv run python tools/arc_disjoint_check.py check --candidate=HEAD' \
         'uv run python tools/arc_disjoint_check.py check --candidate -x' \
         'uv run python tools/arc_disjoint_check.py check --candidate HEAD~1' \
         'HARNESS_LANE_ID=peer uv run python tools/arc_disjoint_check.py check --candidate HEAD' \
         'HARNESS_ARC_ID=x HARNESS_LANE_ID=peer uv run python tools/arc_disjoint_check.py check' \
         'uv run python tools/arc_disjoint_check.py check --pairs x'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "arc_disjoint_check hardening: '$c' → not allow" || bad "arc_disjoint_check over-matched: $c"
done
# (a''') U-HE-31: the reaping recipe's mandatory pre-step. two-lane/SKILL.md now requires the
# lane's stack to come down BEFORE safe-worktree-remove frees its index; if that command is
# ask-then-deny in loop mode, the carrier stalls one step short of the allowlisted reaper.
for c in 'just r420-self-hosted-stack-down' \
         'just r420-self-hosted-stack-up' \
         'just r420-self-hosted-stack-status'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "allow" ] && ok "stack recipe → allow: '$c'" || bad "stack recipe not allowed: $c → $OUT"
done
for c in 'just r420-self-hosted-readiness /etc/passwd' \
         'just r420-self-hosted-stack-down; rm -rf /' \
         'just r420-self-hosted-stack-nuke'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "stack-recipe hardening: '$c' → not allow" || bad "stack recipe over-matched: $c"
done

# (a'') U-HE-31: lane open SOURCES lane-init. Without an allowance the two-lane recipe's
# mandatory first step becomes ask → headless denial, and the lane runs with no id/index.
for c in 'source tools/hooks/lane-init.sh' \
         '. tools/hooks/lane-init.sh' \
         '  source tools/hooks/lane-init.sh'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" = "allow" ] && ok "sourced lane-init → allow: '$c'" || bad "lane-init source not allowed: $c → $OUT"
done
# hardening: the allowance carries NO argument surface and names ONE path.
for c in 'source tools/hooks/lane-init.sh --force' \
         'source tools/hooks/other.sh' \
         'source ../elsewhere/lane-init.sh' \
         'source /etc/lane-init.sh' \
         'sourced tools/hooks/lane-init.sh' \
         'source tools/hooks/lane-init.sh; rm -rf /' \
         'source tools/hooks/lane-init.sh && curl evil.example'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "lane-init hardening: '$c' → not allow" || bad "lane-init over-matched: $c"
done

# (a') ship-pr's pending→open flip: transition is allowed for --to open ONLY (codex r1 P1);
# terminal targets reject the whole command wherever they appear (argparse last-wins).
OUT=$(run_on "$(pl Bash 'uv run python tools/reservations.py transition --arc-id u-he-25 --to open --lane-id lane-1' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "transition --to open → allow" || bad "transition --to open not allowed: $OUT"
for c in 'uv run python tools/reservations.py transition --arc-id x --to merged --lane-id l' \
         'uv run python tools/reservations.py transition --arc-id x --to abandoned --lane-id l' \
         'uv run python tools/reservations.py transition --arc-id x --to open --to merged --lane-id l' \
         'uv run python tools/reservations.py transition --arc-id x --to open --to=merged --lane-id l' \
         'uv run python tools/reservations.py transition --arc-id x --to=open --lane-id l' \
         'uv run python tools/reservations.py transition --arc-id x --t merged --lane-id l' \
         'uv run python tools/reservations.py transition --arc-id x --to open --t abandoned --lane-id l' \
         'uv run python tools/reservations.py transition --arc-id x --lane-id l'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "transition hardening: '$c' → not allow" || bad "transition over-matched: $c"
done
# (b) leading env-prefix strip: EXACTLY HARNESS_ARC_ID= / HARNESS_LANE_ID= with bareword values.
OUT=$(run_on "$(pl Bash 'HARNESS_ARC_ID=u-he-25 HARNESS_LANE_ID=lane-1 just review-with-failover' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "HARNESS_* prefixed review-with-failover → allow" || bad "prefixed review not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'HARNESS_LANE_ID=lane-1 git status' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "single HARNESS_LANE_ID prefix → allow" || bad "single-prefix git status not allowed: $OUT"
# hardening: other HARNESS_* names, $-expansion / quoted values, and prefix-only stay un-allowed
for c in 'HARNESS_FAILOVER_CHILD=1 just gemini-review' \
         'HARNESS_ARC_ID=$ARC just review-with-failover' \
         'HARNESS_ARC_ID="u he" just review-with-failover' \
         'HARNESS_ARC_ID=u-he-25' \
         'HARNESS_ARC_ID=u-he-25 gh pr close 1'; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "env-prefix hardening: '$c' → not allow" || bad "env-prefix over-matched: $c"
done
# prefixed dangerous command still hits the deny-list (strip must not bypass deny scan)
OUT=$(run_on "$(pl Bash 'HARNESS_ARC_ID=u-he-25 git push --force origin main' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "prefixed force-push → still deny" || bad "prefixed force-push not denied: $OUT"
# (c) git merge-tree joins the read-arc git verb group
OUT=$(run_on "$(pl Bash 'git merge-tree --write-tree origin/main HEAD' '')")
[ "$(dec "$OUT")" = "allow" ] && ok "git merge-tree → allow" || bad "git merge-tree not allowed: $OUT"
# in-worktree symlink to an outside file → Read must ask (OS would follow the link out)
ln -sf /etc/passwd "$REPO/secretlink" 2>/dev/null
OUT=$(run_on "$(pl Read '' "$REPO/secretlink")")
[ -z "$OUT" ] && ok "symlink escaping worktree → ask" || bad "symlink-escape Read auto-decided: $OUT"
rm -f "$REPO/secretlink"

# 5p) Round-9: jq env dump + glob content-read → ask (content verbs dropped, codex P1).
for c in "jq -n env" "cat .*" "grep -r TOKEN config/*" "jq -n \$ENV"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
# 5q) symlink CHAIN (link → link → outside) → Read must ask (codex P2 multi-hop).
ln -sf /etc/hosts "$REPO/chain2" 2>/dev/null
ln -sf "$REPO/chain2" "$REPO/chain1" 2>/dev/null
OUT=$(run_on "$(pl Read '' "$REPO/chain1")")
[ -z "$OUT" ] && ok "symlink chain escaping worktree → ask" || bad "symlink-chain Read auto-decided: $OUT"
rm -f "$REPO/chain1" "$REPO/chain2"

# 5r) Round-10: git branch force/move → ask; Glob abs/traversal pattern → ask (codex P1/P2).
for c in "git branch -f main HEAD~1" "git branch -m old new" "git branch -C a b"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed" || bad "'$c' auto-allowed: $OUT"
done
for pat in "/etc/*" "../*.pem"; do
  OUT=$(run_on "$(jq -nc --arg p "$pat" '{"hook_event_name":"PreToolUse","tool_name":"Glob","tool_input":{"pattern":$p}}')")
  [ -z "$OUT" ] && ok "Glob pattern '$pat' → ask" || bad "Glob '$pat' auto-decided: $OUT"
done
# in-worktree Glob (relative pattern, no path) still allows
OUT=$(run_on "$(jq -nc '{"hook_event_name":"PreToolUse","tool_name":"Glob","tool_input":{"pattern":"**/*.py"}}')")
[ "$(dec "$OUT")" = "allow" ] && ok "Glob relative pattern → allow" || bad "relative Glob not allowed: $OUT"

# 5s) Round-11: path mutators/listers dropped from Bash allow (symlink-through escape); broad
#     recursive Grep gated (descendant-secret read). codex P1×2.
for c in "mkdir out/dir" "touch out/file" "chmod +x out/x" "ls out"; do
  OUT=$(run_on "$(pl Bash "$c" '')")
  [ "$(dec "$OUT")" != "allow" ] && ok "'$c' → not auto-allowed (mutator dropped)" || bad "'$c' auto-allowed: $OUT"
done
# broad Grep (no path, or repo root) → ask; specific in-worktree subpath still allows
OUT=$(run_on "$(jq -nc '{"hook_event_name":"PreToolUse","tool_name":"Grep","tool_input":{"pattern":"TODO"}}')")
[ -z "$OUT" ] && ok "broad Grep (no path) → ask" || bad "broad Grep auto-decided: $OUT"
OUT=$(run_on "$(jq -nc --arg p "$REPO" '{"hook_event_name":"PreToolUse","tool_name":"Grep","tool_input":{"path":$p}}')")
[ -z "$OUT" ] && ok "Grep at repo root → ask" || bad "root Grep auto-decided: $OUT"
OUT=$(run_on "$(jq -nc --arg p "$REPO/src" '{"hook_event_name":"PreToolUse","tool_name":"Grep","tool_input":{"path":$p}}')")
[ "$(dec "$OUT")" = "allow" ] && ok "Grep at specific subpath → still allow" || bad "subpath Grep not allowed: $OUT"

# 6) PermissionRequest event uses the decision.behavior schema.
OUT=$(run_on "$(pl Bash 'git status' '' PermissionRequest)")
[ "$(beh "$OUT")" = "allow" ] && ok "PermissionRequest allow schema" || bad "PR allow schema wrong: $OUT"
# U-HE-42: ship-pr's pre-push step must not stall a headless lane at either hook event.
OUT=$(run_on "$(pl Bash 'just codex-context-check-ci' '' PermissionRequest)")
[ "$(beh "$OUT")" = "allow" ] && ok "PermissionRequest allows just codex-context-check-ci" || bad "PR codex-context-check-ci not allowed: $OUT"
OUT=$(run_on "$(pl Bash 'rm -rf /' '' PermissionRequest)")
[ "$(beh "$OUT")" = "deny" ] && ok "PermissionRequest deny schema" || bad "PR deny schema wrong: $OUT"

# 7) §0 worktree-live-session guard — hoisted ABOVE the inert gate, so it DENIES a
#    `git worktree remove` of a worktree with a live session in BOTH loop on/off.
WTL="$REPO/wt-live"; mkdir -p "$WTL"
FH="$REPO/home"
# Encode a worktree path EXACTLY as lib.sh's worktree_has_live_session does (printf '%s'
# strips the trailing newline so the newline is never mapped to a stray '-').
encof() { printf '%s' "$(cd "$1" && pwd -P)" | tr -c '[:alnum:]' '-'; }
SD="$FH/.claude/projects/$(encof "$WTL")"; mkdir -p "$SD"; : > "$SD/s.jsonl"   # fresh = live
runh() { printf '%s' "$1" | env HOME="$FH" CLAUDE_PROJECT_DIR="$REPO" ${2:-} bash "$HOOK"; }
OUT=$(runh "$(pl Bash "git worktree remove $WTL" '')")                       # loop OFF
[ "$(dec "$OUT")" = "deny" ] && ok "live-session worktree remove → deny (loop OFF, hoisted)" || bad "live remove not denied off-mode: $OUT"
OUT=$(runh "$(pl Bash "git -C $REPO worktree remove $WTL" '')")
[ "$(dec "$OUT")" = "deny" ] && ok "live-session git -C worktree remove → deny" || bad "live git -C remove not denied: $OUT"
OUT=$(runh "$(pl Bash "git worktree remove --force $WTL" '')" HARNESS_LOOP=1)  # loop ON, --force
[ "$(dec "$OUT")" = "deny" ] && ok "live-session worktree remove --force → deny (loop ON)" || bad "live --force not denied: $OUT"
OUT=$(runh "$(pl Bash "git worktree remove $WTL" '')" HARNESS_ALLOW_LIVE_WORKTREE_REMOVE=1)
[ "$(dec "$OUT")" != "deny" ] && ok "override env → not denied" || bad "override still denied: $OUT"
# stale/no transcript still requires the mutex-backed wrapper because a session can start
# between a negative check and the eventual tool execution.
WTS="$REPO/wt-stale"; mkdir -p "$WTS"; SDS="$FH/.claude/projects/$(encof "$WTS")"; mkdir -p "$SDS"
: > "$SDS/old.jsonl"; touch -t 202001010000 "$SDS/old.jsonl"
OUT=$(runh "$(pl Bash "git worktree remove $WTS" '')")
[ "$(dec "$OUT")" = "deny" ] && ok "stale-transcript direct removal → denied for race safety" || bad "stale direct removal not denied: $OUT"
# no transcript dir at all → not denied
WTN="$REPO/wt-none"; mkdir -p "$WTN"
OUT=$(runh "$(pl Bash "git worktree remove $WTN" '')")
[ "$(dec "$OUT")" = "deny" ] && ok "no-transcript direct removal → denied for race safety" || bad "no-transcript direct removal not denied: $OUT"

# 8) C-HE-08 §1 (U-HE-26): push-to-main denied in the audited deny block; topic pushes
#    stay auto-allowed. The parser reads the argument list (options skipped anywhere), so
#    multi-option forms the spec's reference regexes missed are covered.
for c in 'git push origin HEAD:main' 'git push origin main' 'git push origin refs/heads/main' 'git push -u origin feature:main' 'git push --set-upstream origin main' 'git push origin feature main' 'git push --force-with-lease=x origin +feature:refs/heads/main' "git push origin 'HEAD:main'" 'git push origin "main"'; do
  OUT=$(run_on "$(pl Bash "$c" '')"); [ "$(dec "$OUT")" = "deny" ] && ok "'$c' → deny" || bad "push-to-main not denied: $c → $OUT"
done
OUT=$(run_on "$(pl Bash 'git push origin feature' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "topic push → allow" || bad "topic push blocked: $OUT"
# Discriminating witness (U-HE-25 rev (v)): the deny must read the prefix-STRIPPED command.
# The allowlist strips HARNESS_ARC_ID=/HARNESS_LANE_ID= into TRIM before its `git push`
# alternation, so a raw-command-anchored deny would let a prefixed push-to-main through to
# auto-ALLOW — this row fails under the raw reading and passes under the stripped one.
OUT=$(run_on "$(pl Bash 'HARNESS_ARC_ID=u-he-26 git push origin HEAD:main' '')")
[ "$(dec "$OUT")" = "deny" ] && ok "prefixed push-to-main → deny (stripped-read witness)" || bad "prefixed push-to-main not denied: $OUT"
# bare `git push` while main is checked out
# Hermetic identity: without -c user.* the commit FAILS on CI (no global git identity),
# HEAD stays unborn, `checkout -b topic` RENAMES the unborn branch, and every later
# `git checkout main` errors `pathspec 'main'` -- the main-checkout deny rows then test
# a topic checkout (CI-only red, 2026-08-21).
( cd "$REPO" && git init -q . && git checkout -q -b main 2>/dev/null; git -c user.email=hermetic@test -c user.name=hermetic commit -q --allow-empty -m i )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "bare push on main checkout → deny" || bad "bare push on main not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push -u origin' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "option-bearing bare push on main → deny" || bad "-u origin on main not denied: $OUT"
( cd "$REPO" && git checkout -q -b topic ); OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare push on topic → allow" || bad "bare push on topic blocked: $OUT"

# codex r1 hardening (all on the topic checkout, so a parser miss would fall through to
# auto-allow): shell dequoting of backslashes, option-supplied remote (--repo=), --all,
# wildcard destination refspec.
for c in 'git push origin HEAD:ma\in' 'git push --all origin' "git push origin 'refs/heads/*:refs/heads/*'"; do
  OUT=$(run_on "$(pl Bash "$c" '')"); [ "$(dec "$OUT")" = "deny" ] && ok "'$c' → deny (hardened)" || bad "hardened case not denied: $c → $OUT"
done
# codex r1 P2: bare push on a topic checkout can still update main through config.
( cd "$REPO" && git config push.default matching )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "bare push under push.default=matching → deny" || bad "matching bare push not denied: $OUT"
( cd "$REPO" && git config push.default current && git config remote.origin.url /tmp/nowhere && git config branch.topic.remote origin && git config branch.topic.merge refs/heads/main )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "upstream merge=main under push.default=current → allow (r3 P2: real push goes topic->topic)" || bad "current-mode upstream-main falsely denied: $OUT"
( cd "$REPO" && git config push.default upstream )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "upstream merge=main under push.default=upstream → deny" || bad "upstream-main bare push not denied: $OUT"
( cd "$REPO" && git config push.default current )
( cd "$REPO" && git config --unset branch.topic.merge && git config remote.origin.push 'refs/heads/topic:refs/heads/main' )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "bare push with remote.origin.push dest=main → deny" || bad "remote-push-refspec bare push not denied: $OUT"
( cd "$REPO" && git config --unset remote.origin.push )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare push on topic after config cleanup → allow (checks are config-driven, not sticky)" || bad "cleaned-up bare push blocked: $OUT"

# codex r2 hardening: matching-refspec push, expansion-capable tokens, separate-value
# options, and the full push-remote resolution chain.
OUT=$(run_on "$(pl Bash 'git push origin +:' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "matching-refspec push (+:) → deny" || bad "+: not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push origin HEAD:ma{in,ster}' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "brace-expansion refspec → deny" || bad "brace refspec not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push origin HEAD:ma?n' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "glob-capable dest (?) → deny" || bad "glob dest not denied: $OUT"
( cd "$REPO" && git checkout -q main )
OUT=$(run_on "$(pl Bash 'git push -o ci.skip origin' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "-o value not a positional: bare push on main → deny" || bad "-o bare push on main not denied: $OUT"
( cd "$REPO" && git checkout -q topic )
OUT=$(run_on "$(pl Bash 'git push -o ci.skip origin' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "-o value skipped: bare push on topic → allow" || bad "-o topic push blocked: $OUT"
( cd "$REPO" && git config branch.topic.pushRemote staging && git config remote.staging.push 'refs/heads/topic:refs/heads/main' )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "branch.<b>.pushRemote resolution → deny" || bad "pushRemote not resolved: $OUT"
( cd "$REPO" && git config --unset branch.topic.pushRemote && git config remote.pushDefault staging )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "remote.pushDefault resolution → deny" || bad "pushDefault not resolved: $OUT"
( cd "$REPO" && git config --unset remote.pushDefault && git config --unset remote.staging.push )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare push on topic after r2 config cleanup → allow" || bad "r2 cleaned-up bare push blocked: $OUT"

# codex r3 hardening: separate-value --recurse-submodules, colonless HEAD refspec,
# --repo remote selection, remote.<r>.mirror, configured matching refspec.
( cd "$REPO" && git checkout -q main )
OUT=$(run_on "$(pl Bash 'git push --recurse-submodules no origin' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "--recurse-submodules value-form bare push on main → deny" || bad "recurse-submodules bare push on main not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push origin HEAD' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "colonless HEAD refspec on main checkout → deny" || bad "HEAD refspec on main not denied: $OUT"
( cd "$REPO" && git checkout -q topic )
OUT=$(run_on "$(pl Bash 'git push --recurse-submodules no origin' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "--recurse-submodules value-form bare push on topic → allow" || bad "recurse-submodules topic push blocked: $OUT"
OUT=$(run_on "$(pl Bash 'git push origin HEAD' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "colonless HEAD refspec on topic → allow" || bad "HEAD refspec on topic blocked: $OUT"
OUT=$(run_on "$(pl Bash 'git push origin topic:HEAD' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "explicit :HEAD destination (remote default branch) → deny" || bad ":HEAD dest not denied: $OUT"
( cd "$REPO" && git config remote.origin.push 'refs/heads/topic:refs/heads/main' && git config branch.topic.pushRemote staging )
OUT=$(run_on "$(pl Bash 'git push --repo origin' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "--repo value selects the remote (origin's refspec dest=main) → deny" || bad "--repo remote selection missed: $OUT"
( cd "$REPO" && git config --unset branch.topic.pushRemote && git config --unset remote.origin.push )
( cd "$REPO" && git config remote.origin.mirror true )
OUT=$(run_on "$(pl Bash 'git push origin topic' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "remote.origin.mirror=true refspec push → deny" || bad "mirror-config refspec push not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "remote.origin.mirror=true bare push → deny" || bad "mirror-config bare push not denied: $OUT"
( cd "$REPO" && git config --unset remote.origin.mirror )
( cd "$REPO" && git config remote.origin.push '+:' )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "configured matching refspec (+:) → deny" || bad "configured +: not denied: $OUT"
( cd "$REPO" && git config --unset remote.origin.push )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare push on topic after r3 config cleanup → allow" || bad "r3 cleaned-up bare push blocked: $OUT"

# codex r4 hardening (fail-closed closure moves): quoted-whitespace boundaries, unknown /
# abbreviated options, git-boolean mirror values.
( cd "$REPO" && git checkout -q main )
OUT=$(run_on "$(pl Bash "git push --repo 'remote bare repo'" '')"); [ "$(dec "$OUT")" = "deny" ] && ok "quoted value with whitespace → deny (word-split boundaries unprovable)" || bad "quoted-whitespace push not denied: $OUT"
( cd "$REPO" && git checkout -q topic )
OUT=$(run_on "$(pl Bash 'git push --al origin' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "abbreviated long option (--al == --all) → deny (unknown options fail closed)" || bad "abbreviated --al not denied: $OUT"
( cd "$REPO" && git config remote.origin.mirror yes )
OUT=$(run_on "$(pl Bash 'git push origin topic' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "remote.origin.mirror=yes (git boolean) → deny" || bad "mirror=yes not denied: $OUT"
( cd "$REPO" && git config --unset remote.origin.mirror )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare push on topic after r4 config cleanup → allow" || bad "r4 cleaned-up bare push blocked: $OUT"

# codex r5 hardening: quoted --repo value dequoted before config lookup; DWIM partial
# ref (heads/main) destination.
OUT=$(run_on "$(pl Bash 'git push origin topic:heads/main' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "DWIM partial ref dest (heads/main) → deny" || bad "heads/main dest not denied: $OUT"
( cd "$REPO" && git config remote.origin.push 'refs/heads/topic:refs/heads/main' )
OUT=$(run_on "$(pl Bash "git push --repo 'origin'" '')"); [ "$(dec "$OUT")" = "deny" ] && ok "quoted --repo value dequoted before config lookup → deny" || bad "quoted --repo value missed: $OUT"
( cd "$REPO" && git config --unset remote.origin.push )
OUT=$(run_on "$(pl Bash 'git push' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare push on topic after r5 config cleanup → allow" || bad "r5 cleaned-up bare push blocked: $OUT"

# codex r6: comment tokens defeat word-splitting (deny); remote slot no longer scanned as
# a refspec at >=2 positionals (a remote named `main` must not hard-deny a topic push).
( cd "$REPO" && git checkout -q main )
OUT=$(run_on "$(pl Bash 'git push # comment' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "comment-bearing push (bare push in disguise) → deny" || bad "comment push not denied: $OUT"
( cd "$REPO" && git checkout -q topic )
OUT=$(run_on "$(pl Bash 'git push main topic' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "remote named main + topic refspec → allow (r6 P2: remote slot not a refspec)" || bad "remote-named-main topic push blocked: $OUT"
OUT=$(run_on "$(pl Bash 'git push main main' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "remote named main + main refspec → still deny" || bad "main refspec via main remote not denied: $OUT"

# codex r7: backslash-escaped whitespace is ONE git argument but two word-split tokens
# (deny); the --repo positional-precedence claim was REFUTED by measurement (a positional
# repository beats --repo), so `git push --repo=origin main topic` stays modeled as
# remote=main + refspec=topic.
( cd "$REPO" && git checkout -q main )
OUT=$(run_on "$(pl Bash 'git push origin\ repo' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "backslash-escaped whitespace remote (bare push in disguise) → deny" || bad "backslash-space push not denied: $OUT"
( cd "$REPO" && git checkout -q topic )
OUT=$(run_on "$(pl Bash 'git push --repo=origin main topic' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "--repo + positional repo named main + topic refspec → allow (r7 refutation witness)" || bad "--repo positional-precedence case blocked: $OUT"

# codex r8: recursive submodule modes spawn nested pushes outside the hook (deny);
# `--` end-of-options is not an unknown option (legit topic pushes stay allowed).
OUT=$(run_on "$(pl Bash 'git push --recurse-submodules=on-demand origin topic' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "--recurse-submodules=on-demand → deny (nested pushes unhookable)" || bad "on-demand recurse not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push --recurse-submodules on-demand origin' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "--recurse-submodules on-demand (value form) → deny" || bad "on-demand value form not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push --recurse-submodules=no origin topic' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "--recurse-submodules=no topic push → allow" || bad "=no topic push blocked: $OUT"
OUT=$(run_on "$(pl Bash 'git push -- origin topic' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "-- end-of-options + topic refspec → allow (r8 P2)" || bad "-- topic push blocked: $OUT"
OUT=$(run_on "$(pl Bash 'git push -- origin main' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "-- end-of-options + main refspec → still deny" || bad "-- main refspec not denied: $OUT"

# codex r9: glob chars gate the whole command (pathname expansion applies to every word);
# a single positional is git's REPOSITORY (measured r7) -- `git push main` / `--repo=origin
# main` are bare pushes to a remote named main: denied on a main checkout, allowed on topic.
OUT=$(run_on "$(pl Bash 'git push [am]* topic' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "glob remote token → deny (whole-command glob gate)" || bad "glob remote not denied: $OUT"
( cd "$REPO" && git checkout -q main )
OUT=$(run_on "$(pl Bash 'git push main' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "bare push to remote 'main' on main checkout → deny" || bad "bare push to remote main on main not denied: $OUT"
OUT=$(run_on "$(pl Bash 'git push --repo=origin main' '')"); [ "$(dec "$OUT")" = "deny" ] && ok "--repo=origin main on main checkout → deny (bare push of main)" || bad "--repo bare push on main not denied: $OUT"
( cd "$REPO" && git checkout -q topic )
OUT=$(run_on "$(pl Bash 'git push main' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "bare topic push to remote named 'main' → allow (r9 P2: positional[0] is the repository)" || bad "remote-named-main bare topic push blocked: $OUT"
OUT=$(run_on "$(pl Bash 'git push --repo=origin main' '')"); [ "$(dec "$OUT")" = "allow" ] && ok "--repo=origin main on topic → allow (bare push to repository 'main', measured r7)" || bad "--repo bare topic push blocked: $OUT"

# codex r10 terminal (register-and-hold at the round cap): the metachar gates OVER-DENY
# exotic-but-legit forms BY DESIGN -- quote-aware parsing is exactly what r10 P1 measured
# as unreliable, so a literal '#' inside a quoted push-option stays denied (fail-closed).
OUT=$(run_on "$(pl Bash "git push -o 'note=#123' origin topic" '')"); [ "$(dec "$OUT")" = "deny" ] && ok "quoted '#' push-option → deny (retained fail-closed over-deny, r10 terminal)" || bad "quoted-# over-deny witness failed: $OUT"

# U-SR-03 (codex r7 P2): the subagent tool is named `Agent` on this runtime, and the
# allowlist had drifted off it -- it named only the old `Task`. That is load-bearing now:
# WR-08 makes prompt authoring a delegated Agent call, so a fall-through here is an approval
# prompt, and an approval prompt in a headless lane is a stall. Both names are asserted, and
# an unrelated tool is asserted to still fall through, so this cannot pass by allowing
# everything.
for _t in Agent Task TodoWrite; do
  OUT=$(run_on "$(pl "$_t" '' '')")
  [ "$(dec "$OUT")" = "allow" ] \
    && ok "loop mode auto-allows the $_t tool" \
    || bad "$_t is not auto-allowed in loop mode -- a headless delegation would stall"
done
OUT=$(run_on "$(pl WebFetch '' '')")
[ -z "$(dec "$OUT")" ] \
  && ok "an unrelated tool still falls through (the allowlist is not blanket)" \
  || bad "WebFetch was auto-allowed: the tool allowlist is too broad"
OUT=$(run_off "$(pl Agent '' '')")
[ -z "$OUT" ] && ok "Agent is inert when loop mode is off" || bad "Agent decided off-mode: $OUT"

# codex r10 P1: the r7 allow was justified by "the subagent's own calls re-enter this hook",
# which is FALSE for `isolation: worktree` -- that creates a git worktree as part of the spawn,
# before any child call reaches PreToolUse. The auto-allow is restricted to the shape the
# argument covers, and both isolation modes must fall through to ask. The r7 witness used a
# prompt-only payload and could not see this input shape at all.
agent_pl() { jq -nc --arg i "$1" '{"hook_event_name":"PreToolUse","tool_name":"Agent","tool_input":({"prompt":"go"} + (if $i == "" then {} else {"isolation":$i} end))}'; }
OUT=$(run_on "$(agent_pl '')")
[ "$(dec "$OUT")" = "allow" ] \
  && ok "Agent without isolation → allow (the loop's delegation path)" \
  || bad "a plain Agent spawn is not auto-allowed: $OUT"
for _iso in worktree remote; do
  OUT=$(run_on "$(agent_pl "$_iso")")
  [ -z "$(dec "$OUT")" ] \
    && ok "Agent with isolation=$_iso → ask (spawn-time filesystem mutation)" \
    || bad "isolation=$_iso was auto-allowed: $OUT"
done

echo "----"
echo "permission_guard: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
