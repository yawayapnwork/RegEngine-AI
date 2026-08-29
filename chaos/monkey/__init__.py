"""Compliance Chaos Monkey: application-level fault injection for
RegEngine AI staging environments, distinct from chaos/experiments (K8s
Chaos Mesh/Litmus infra-level network/pod faults) and chaos/load
(throughput generators) -- this package injects semantically corrupted
compliance artifacts (a mutated policy operator, a dropped hash-chain
write, a truncated regulatory PDF) directly against the real
application code paths (app.compiler, app.ledger, app.parsing) to
verify RegEngine's own defense-in-depth catches or safely contains each
one, rather than exercising infrastructure resilience.

See chaos.monkey.runner.ChaosMonkeyRunner for the entrypoint, and
chaos.monkey.postmortem for the automated report each run produces.
"""
