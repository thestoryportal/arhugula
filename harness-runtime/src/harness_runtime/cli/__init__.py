"""Operator-facing CLI for the multi-LLM agent harness.

C-RT-29 per runtime spec v1.35 §14.18; U-RT-102 cluster-root scaffolding.
"""

from harness_runtime.cli.app import app, main

__all__ = ["app", "main"]
