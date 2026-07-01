"""End-to-end integration tests (tier 3 per session-3 §7).

Currently houses U-RT-49 (bootstrap → shutdown smoke). U-RT-50 per-stage
isolation tests will land here in a follow-on unit. Tier-2 marker gating
(`--runtime-integration`) is a future-phase concern; these tests run by
default at HEAD.
"""
