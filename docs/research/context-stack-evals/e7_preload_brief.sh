#!/usr/bin/env bash
# E7 — does a Graft card brief replace the full guide for a subagent, at lower preload?
#
# Two conditions per question, both non-interactive `claude -p` (subscription, no API):
#   full : cwd = the repo, default context (root CLAUDE.md, memory, hooks all load)
#   brief: cwd = a scratch dir, a minimal system prompt + `graft map` orientation as the
#          only repo context, repo reachable via --add-dir
# Recorded per run: usage tokens, turns, duration, the answer. Correctness judged after.
set -u
REPO=$(cd "$(dirname "$0")/../../.." && pwd -P)  # the checkout this script lives in
OUT=$REPO/docs/research/context-stack-evals/E7
SCRATCH=$(mktemp -d)
mkdir -p "$OUT" "$SCRATCH"

MAP=$(cd "$REPO" && graft map --budget 1500) || { echo "graft map failed; brief would carry no orientation" >&2; exit 1; }
BRIEF="You answer one question about the Python repository at $REPO using only its files. Cite the exact path and line. Use Read, Grep and rg only. Stop when you have the evidence. Repo orientation from its call graph follows.

$MAP"

RULES='Cite exact evidence: a file path and line number, or for markdown a §section. Quote the decisive line (at most 20 words). Do not guess; say NOT FOUND if you cannot find it. Stop as soon as you have the evidence. Respond with exactly three lines: ANSWER: ... / EVIDENCE: path:line — "quote" / TOOLS_USED: N'

declare -a QS=(
  "Q01|What are the two conditions that make a PR a terminating refresh PR under the roadmap protocol?"
  "Q06|Which model and reasoning effort does just codex-review pin per the root CLAUDE.md?"
  "Q10|Which harness-is source file defines the PathClass enum, and at what line?"
  "Q15|Which tools module defines the class WorkspaceState, and at what line?"
)

for entry in "${QS[@]}"; do
  qid=${entry%%|*}; q=${entry#*|}
  ( cd "$REPO" && printf '%s\n\nQUESTION: %s\n' "$RULES" "$q" | claude -p --model sonnet --output-format json \
      --allowedTools "Read,Grep,Glob,Bash(rg:*)" > "$OUT/full_$qid.json" 2> "$OUT/full_$qid.err" )
  ( cd "$SCRATCH" && printf '%s\n\nQUESTION: %s\n' "$RULES" "$q" | claude -p --model sonnet --output-format json \
      --system-prompt "$BRIEF" --add-dir "$REPO" \
      --allowedTools "Read,Grep,Glob,Bash(rg:*)" > "$OUT/brief_$qid.json" 2> "$OUT/brief_$qid.err" )
  echo "done $qid"
done
echo "E7 runs complete"
