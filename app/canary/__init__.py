"""Shadow execution and canary release for OPA Rego policies: mirrors
live trade evaluation traffic to a candidate policy running alongside
the active production policy, compares outcomes/latency in real time,
and automatically promotes or rolls back the candidate.

Distinct from `app.backtest` (offline replay of PAST ledger
transactions against a separate OPA instance, on demand) -- this
package shadows LIVE transactions in real time, in-process, and can
change what's live on its own via the same
`app.execution.policy_publisher.PolicyPublisher` any human-approved
publish goes through. A candidate is never live-affecting: it runs
under a namespaced package/rule_id (`app.canary.opa_publisher`) on the
SAME production OPA server, evaluated read-only in a fire-and-forget
task that never blocks or can fail the real decision path
(`app.canary.mirroring`).

There is no real Kafka topic anywhere in this codebase (confirmed
during design: `flink/` and `cdc/` at the repo root are standalone
reference scripts, not wired into this FastAPI/Celery app) -- "traffic
shadowing" here means duplicating a transaction's already-built OPA
`input_doc` at the exact call site inside `app.execution.evaluator.Evaluator`,
not consuming a second copy off a message bus.

Gated behind `settings.canary_enabled` (default False).
"""
