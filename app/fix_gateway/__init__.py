"""FIX Protocol gateway: intercepts New Order Single (35=D) messages
from broker OMS/RMS engines and validates them against compiled SEBI
policy, returning an immediate Execution Report (35=8) with a
SEBI-clause-mapped rejection when non-compliant.

The sub-500-microsecond validation budget this subsystem targets is
NOT achievable through `app.execution.evaluator.Evaluator`/
`app.execution.opa_engine.OPAEngine` -- that module's own docstring
states OPA answers "in low single-digit milliseconds" over even a
loopback HTTP call, three to four orders of magnitude over budget.
This package instead loads the SAME compiled policy (its `jsonlogic_ast`
representation, already produced by `app.compiler.jsonlogic_compiler`
for exactly this "fallback representation for microservices that don't
run OPA" purpose) into `native/`'s pre-existing, already-benchmarked
allocation-free C++ policy kernel, real numbers for which are recorded
in `native/benchmarks/bench_fix_gateway.cpp`'s header comment (p50 ~400ns,
p99 ~600ns end to end -- roughly 800-1000x under budget at steady state).

Two integration surfaces exist, deliberately kept separate:
  - `native/include/regengine/fix_gateway.h` -- the actual hot path, a
    pure C++ header a co-located C++ OMS/RMS compiles directly against.
  - `app/fix_gateway/gateway_application.py` -- a QuickFIX/Python
    `Application` for a Python-hosted OMS component or session/test
    harness. This calls into the SAME compiled policy via the
    `regengine_native` pybind11 binding (native/bindings/pybind_module.cpp),
    whose own docstring is explicit that a pybind11/Python call is NOT
    the sub-microsecond path -- expect low tens-of-microseconds to low
    hundreds-of-microseconds here, dominated by CPython/QuickFIX-Python
    overhead, not by the underlying evaluate() call itself. Use this
    integration where a Python-hosted OMS's own per-order budget makes
    that overhead negligible; use the C++ header directly where it
    doesn't.

Gated behind `settings.fix_gateway_enabled` (default False).
"""
