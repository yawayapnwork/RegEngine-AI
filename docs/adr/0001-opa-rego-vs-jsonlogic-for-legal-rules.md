# ADR 001: OPA Rego as the Canonical Policy Language, with a Restricted JSON-Logic Fallback

## Status

Accepted

## Context

Every SEBI compliance rule RegEngine AI's dual-agent extraction pipeline
(`app.agents`) produces (`ExtractedComplianceRule`, with numeric
thresholds, entity scoping, trigger conditions, and qualitative
directives) must ultimately become something an execution engine can
evaluate against a live broker transaction in real time.

Two fundamentally different execution shapes exist in this platform and
both need a compiled artifact:

1. **The synchronous evaluation path** (`app.execution.evaluator.Evaluator`
   → `app.execution.opa_engine.OPAEngine`) — a co-located OPA server,
   hot-reloadable via its Policy API, answering in low single-digit
   milliseconds over a loopback HTTP call. This is the platform's
   general-purpose compliance gate: it must express arbitrary logic
   (entity scoping, multi-condition AND/OR, range checks, qualitative
   triggers), not just flat numeric comparisons.
2. **The FIX gateway hot path** (`native/`, `app.fix_gateway`) — a
   sub-500-microsecond order-validation budget that OPA's own HTTP
   round trip cannot reach by three to four orders of magnitude (see
   `app.execution.opa_engine`'s own docstring: "low single-digit
   milliseconds"). This path needs an allocation-free, zero-network,
   in-process evaluator instead.

A single policy representation cannot serve both needs well: a format
expressive enough for arbitrary Rego logic is too complex to flatten
into a fixed-size, pointer-chasing-free native struct; a format
restricted enough for a sub-microsecond native kernel cannot express
Rego's full conditional/string/aggregate vocabulary.

## Decision

**OPA Rego is the canonical, primary compiled representation of every
compliance rule** (`app.compiler.rego_compiler.compile_rule_to_rego`),
published to and evaluated by a co-located OPA server for the general
synchronous/batch/CDC execution paths.

**A deliberately restricted JSON-Logic AST is compiled as a secondary,
mechanically-derived representation**
(`app.compiler.jsonlogic_compiler.compile_rule_to_jsonlogic`), scoped
to exactly the grammar a rule's `deterministic_logic` needs: `and`,
`==`, `>=`, `>`, `<=`, `<`, and a single `in`/`==` entity-type equality
gate. This is never a second, independent policy compiler with its own
opinions about what a rule means — both compilers walk the same
`ExtractedComplianceRule.deterministic_logic`/`target_entities` input
and must produce provably equivalent decisions for any rule the
JSON-Logic compiler accepts at all (verified by
`app.backtest.jsonlogic_evaluator` and `native/`'s C++ kernel both
documenting themselves as "bit-compatible" with this exact grammar).

The JSON-Logic AST exists specifically to serve consumers Rego/OPA
cannot serve well:

- `native/`'s allocation-free C++ kernel (`policy_engine.h`) flattens
  it, at policy-load time only, into a fixed array of
  `(field_slot, operator, threshold)` triples — the representation
  that makes the FIX gateway's measured p50 ≈ 400ns / p99 ≈ 600ns
  possible (`native/benchmarks/bench_fix_gateway.cpp`).
- `app.backtest.jsonlogic_evaluator` replays historical transactions
  against a candidate policy without paying an OPA round trip per
  historical row.
- Any future non-Python/non-OPA microservice (a Node service using
  `json-logic-js`, a Java service using `json-logic-java`) can evaluate
  the identical compiled decision without embedding an OPA runtime at
  all.

A rule whose logic falls outside this restricted grammar (e.g. it uses
`qualitative_directives` with no deterministic threshold, or a
multi-entity `in` clause with mixed conditions the packager can't
express as a single hash) compiles to Rego only — `pack_policy.py`
raises `UnsupportedPolicyShapeError` rather than silently approximating
it, and that rule is simply absent from the fast-path native/JSON-Logic
consumers. It remains fully enforced through OPA.

## Alternatives Considered

- **JSON-Logic (or an equivalent flat rule format) as the ONLY policy
  language.** Rejected: cannot express Rego's full compliance
  vocabulary (nested conditionals, qualitative triggers, cross-clause
  references, custom aggregate functions a future SEBI circular might
  require), and would make the general-purpose OPA execution path the
  one paying for a restriction it doesn't need.
- **Rego as the ONLY policy language, compiled to WebAssembly for the
  hot path (`opa build -t wasm`) instead of a bespoke native kernel.**
  Considered seriously. Rejected for the literal sub-microsecond FIX
  gateway target specifically: a general Rego/Wasm interpreter still
  pays bytecode dispatch, bounds checks, and a generic value
  representation per evaluation — real costs a flat array of pre-resolved
  `(field_slot, operator, threshold)` triples never incurs (see
  `native/include/regengine/policy_types.h`'s module docstring for the
  measured reasoning). Rego/Wasm remains a reasonable choice for a
  future consumer whose latency budget is merely "no HTTP round trip,"
  rather than "under 500 microseconds end to end."
- **A single shared intermediate representation (e.g. compile Rego to
  JSON-Logic, or vice versa, via a generic transpiler) instead of two
  independent compilers reading the same source.** Rejected: a
  transpiler between two different-expressiveness languages either has
  to reject the same rules a direct compiler would (no simpler than
  what exists today) or silently drop expressiveness in one direction
  — a correctness risk for a compliance platform. Compiling both
  representations directly from the same
  `ExtractedComplianceRule.deterministic_logic` keeps each compiler
  simple and independently testable against the same source of truth.

## Consequences

- Every compiled rule potentially has TWO artifacts
  (`CompiledRule.rego_policy` and `CompiledRule.jsonlogic_ast`), and
  they must be kept in lock-step by construction (both read the exact
  same `NumericalThreshold`/`TargetEntity` fields via
  `app.compiler.naming.metric_field_name`) — a change to one compiler's
  field-naming convention without the matching change to the other
  would silently desynchronize what `input.facts.<field>` means between
  the two evaluation paths. This is caught in this codebase by
  `native/tests/test_policy_engine.cpp` and
  `native/tests/test_pack_and_native.py` embedding real, captured
  `pack_policy()` output as frozen fixtures.
- Not every compliant rule has a fast-path JSON-Logic/native
  representation — a rule using qualitative directives or logic outside
  the restricted grammar is enforced ONLY through OPA. This is by
  design (see Decision), but means the FIX gateway's coverage of "every
  compiled SEBI rule" is a strict subset of OPA's coverage, not
  parity — a gap the platform must monitor (e.g. via
  `app.fix_gateway.hot_reload`'s logged "not packageable for the native
  fast path" outcome) rather than assume away.
- `pack_policy.py`'s RPKB1 binary format is now a durable interface
  contract of its own (documented in `native/include/regengine/c_api.h`
  and `native/tools/pack_policy.py`): changing `ThresholdCheck`'s
  layout is a binary-format break, not just a recompile, per that
  struct's own `static_assert`.
