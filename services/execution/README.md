# Execution Service

Evaluates live broker transactions against compiled OPA policy via co-located
OPA server over HTTP (with in-process L1 policy cache and experimental native C++
kernel in `native/` for sub-millisecond evaluation, and client-side OPA Wasm in
the frontend playground), backed by Redis for the policy registry and HITL queue.

Run locally: `uvicorn app.main:app --reload --port 8004`
