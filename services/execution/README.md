# Execution Service

Evaluates live broker transactions against compiled OPA policy
(co-located server, or a Wasm-compiled bundle embedded in-process for
the sub-millisecond hot path), backed by Redis for the policy registry
and HITL queue.

Run locally: `uvicorn app.main:app --reload --port 8004`
