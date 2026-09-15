#!/usr/bin/env bash
# Codex+Advisor decision-resolver library (U-HK-13). Sourced by the /resolve skill
# flow and its test. Provides a bounded out-of-family codex one-shot query (on the
# ChatGPT subscription — $0, NOT metered) plus ledger helpers for recording an
# auto-decision (reviewers agreed) or a split (reviewers disagreed → safer default).
#
# Decision authority lives in the skill (Claude runs codex + advisor and compares);
# this lib is the mechanical substrate: invoke codex, write the audit row. Depends on
# lib.sh (hook_bounded) + loop_lib.sh (loop_log). Test: tools/hooks/test_resolve_lib.sh.

# One-shot codex query, out-of-family. Echoes codex's answer; bounded (codex deep runs
# can take many minutes). `env -u OPENAI_API_KEY` hides the metered key so codex uses
# the subscription auth and cannot silently fall back to a paid key (same discipline as
# the justfile codex-review recipe). Returns 2 if codex is absent.
resolve_codex() { # $1 = prompt
  command -v codex >/dev/null 2>&1 || { echo "[resolve] codex CLI absent" >&2; return 2; }
  hook_bounded "${RESOLVE_CODEX_TIMEOUT:-900}" \
    env -u OPENAI_API_KEY codex exec -c model="gpt-5.6-sol" -c model_reasoning_effort="medium" -c preferred_auth_method="chatgpt" "$1"
}

# Record an auto-decision after Codex+Advisor AGREED. Usage: resolve_record <decision> <rationale>
resolve_record() { loop_log RESOLVE "DECIDE: $1 — ${2:-}"; }

# Record a SPLIT (Codex vs Advisor disagreed) + the safer/reversible default taken, for
# later human review. Usage: resolve_split <safe_default> <detail>
resolve_split() { loop_log RESOLVE-SPLIT "SAFE-DEFAULT: $1 — ${2:-}"; }
