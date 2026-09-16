FINDINGS: 1
- [P2] tools/hooks/test_permission_guard.sh:801 — PermissionRequest-schema coverage for the new allowlist branch exercises only the `-ci` suffix, not the bare command it is alternated with.
  scenario: permission-guard.sh:907 changed `context-check` to `context-check(-ci)?`; the bare form is asserted only in the PreToolUse loop; dropping the `?` passes every PermissionRequest assertion while removing the bare command from that event's allowlist.
PACK_USED: no
