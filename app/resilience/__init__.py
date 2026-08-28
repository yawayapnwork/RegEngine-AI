"""Failure recovery: a Redis-backed Dead-Letter Queue (dead_letter_queue.py)
for edge-case processing failures the pipeline cannot recover from on its
own, plus the retry-classification helpers (retry_policy.py) that decide
whether a given failure is worth retrying at all before it gets there."""
