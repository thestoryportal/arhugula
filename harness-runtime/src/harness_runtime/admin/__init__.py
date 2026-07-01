"""`harness_runtime.admin` — admin CLI stubs (C-RT-13).

Track A admin surface (both landed):

- `harness_runtime.admin.inspect` (U-RT-47) — read-only state-ledger summary.
- `harness_runtime.admin.shutdown_cli` (U-RT-48) — signal-running-instance.
- `harness_runtime.admin.pidfile` (U-RT-48) — pidfile IPC primitive (write at
  stage 7; remove at end of `shutdown()`; read by `harness-shutdown`).

Both stubs are CLI-only (`[project.scripts]` in pyproject.toml). Richer
admin IPC is Track B per spec §13.
"""
