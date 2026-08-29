"""Requirement 1 -- Error Interception: turns a raw OPA/JSON-Logic
failure into a typed `PolicyFailure`, and runs the isolated test cases
Requirement 3 asks for before a repair is trusted.

No `opa` CLI/binary is available in this environment (this codebase's
own tests substitute SQLite for Postgres and mock httpx transports for
external services rather than requiring live infrastructure -- see
app.execution.opa_engine's module docstring on why OPA runs as a
separate server, never embedded), so real OPA compile-error detection
here means catching `OPAEngineError` from a REAL `OPAEngine.publish_policy`
call (genuinely testable against `httpx.MockTransport`, which
constructs a real `httpx.AsyncClient` and exercises the real request/
response code path -- see tests/test_healing.py) plus a deterministic
static structural check for the error classes that don't need a live
OPA compiler to catch at all (unbalanced braces, a missing `package`
line, a missing `default allow`).
"""
from __future__ import annotations

import traceback as tb_module

from app.backtest.jsonlogic_evaluator import MissingFactError, UnsupportedJsonLogicNodeError, evaluate_jsonlogic
from app.compiler.jsonlogic_validator import validate_json_logic_ast
from app.compiler.models import CompiledRego
from app.execution.opa_engine import OPAEngine, OPAEngineError
from app.healing.models import PolicyErrorType, PolicyFailure, TestCaseResult

# OPA's own error-code prefixes (stable, documented parts of its compiler's
# error strings -- e.g. "1 error occurred: policy.rego:4: rego_parse_error:
# unexpected assign token"). Order matters: check the more specific
# "unsafe"/"type" codes before the generic "compile_error" substring some
# of them also happen to contain.
_OPA_ERROR_CODE_MAP: tuple[tuple[str, PolicyErrorType], ...] = (
    ("rego_parse_error", PolicyErrorType.SYNTAX_ERROR),
    ("rego_unsafe_var_error", PolicyErrorType.COMPILE_ERROR),
    ("rego_type_error", PolicyErrorType.COMPILE_ERROR),
    ("rego_recursion_error", PolicyErrorType.COMPILE_ERROR),
    ("rego_compile_error", PolicyErrorType.COMPILE_ERROR),
)


def classify_opa_error_message(message: str) -> PolicyErrorType:
    """OPA's Policy API returns its compiler's error text verbatim in a
    non-2xx response body (see `OPAEngine.publish_policy`'s
    `OPAEngineError`); this parses that text into one of Requirement 1's
    three failure classes. Falls back to COMPILE_ERROR (the safest
    default -- treating an unrecognized message as a syntax error would
    make the deterministic fixer try string-level "fixes" against
    something it doesn't understand) when no known code is found."""
    lowered = message.lower()
    for code, error_type in _OPA_ERROR_CODE_MAP:
        if code in lowered:
            return error_type
    return PolicyErrorType.COMPILE_ERROR


def check_rego_structure(rego_code: str) -> list[str]:
    """Deterministic static checks that don't need a live OPA compiler.
    Returns a list of human-readable issues (empty = nothing found by
    these specific checks -- NOT a guarantee the Rego is otherwise
    valid; this is a cheap first pass, not a substitute for `opa check`
    in a real deployment that has the binary available)."""
    issues: list[str] = []

    pairs = {"{": "}", "(": ")", "[": "]"}
    closers = {v: k for k, v in pairs.items()}
    stack: list[str] = []
    in_string = False
    string_char = ""
    for i, ch in enumerate(rego_code):
        if in_string:
            if ch == string_char and rego_code[i - 1] != "\\":
                in_string = False
            continue
        if ch in ("\"", "`"):
            in_string, string_char = True, ch
        elif ch in pairs:
            stack.append(ch)
        elif ch in closers:
            if not stack or stack[-1] != closers[ch]:
                issues.append(f"Unbalanced '{ch}' at character offset {i} (no matching '{closers[ch]}' open on the stack).")
                break
            stack.pop()
    if not in_string and stack:
        issues.append(f"Unclosed {stack} -- {len(stack)} bracket/brace/paren(s) never closed.")

    if "package " not in rego_code:
        issues.append("Missing 'package <name>' declaration.")

    if "default allow" not in rego_code and "allow :=" not in rego_code and "allow if" not in rego_code:
        issues.append("No 'allow' rule found (neither a default nor a conditional one) -- OPA would evaluate this policy as always-undefined.")
    elif "default allow" not in rego_code:
        issues.append(
            "Missing 'default allow := false'. Without a default, `allow` is UNDEFINED (not false) whenever no "
            "conditional `allow` rule matches -- a materially different, less safe semantics than the rest of "
            "this codebase's compiled policies (see app.compiler.rego_compiler's module docstring: "
            "'safe-by-default: missing data denies, never silently permits')."
        )

    return issues


def build_failure_from_opa_error(
    *,
    rule_id: str,
    circular_number: str | None,
    clause_number: str | None,
    source_clause_text: str,
    rego_code: str,
    exc: OPAEngineError,
    attempt_number: int = 0,
) -> PolicyFailure:
    """The Requirement-1 entrypoint for the publish path: wrap a call to
    `OPAEngine.publish_policy` and, on failure, produce a `PolicyFailure`
    ready to hand to the repair loop."""
    return PolicyFailure(
        rule_id=rule_id,
        circular_number=circular_number,
        clause_number=clause_number,
        source_clause_text=source_clause_text,
        rego_code=rego_code,
        error_type=classify_opa_error_message(str(exc)),
        error_message=str(exc),
        stack_trace="".join(tb_module.format_exception(type(exc), exc, exc.__traceback__)),
        attempt_number=attempt_number,
    )


async def attempt_publish_and_intercept(
    opa: OPAEngine,
    compiled: CompiledRego,
    *,
    circular_number: str | None,
    clause_number: str | None,
    source_clause_text: str,
    attempt_number: int = 0,
) -> PolicyFailure | None:
    """Returns None on a clean publish; a `PolicyFailure` if OPA rejected
    it. Never raises `OPAEngineError` itself -- that is the exact
    condition this function exists to catch."""
    try:
        await opa.publish_policy(compiled)
    except OPAEngineError as exc:
        return build_failure_from_opa_error(
            rule_id=compiled.rule_id,
            circular_number=circular_number,
            clause_number=clause_number,
            source_clause_text=source_clause_text,
            rego_code=compiled.rego_code,
            exc=exc,
            attempt_number=attempt_number,
        )
    return None


def run_isolated_test_cases(json_logic: dict, test_fixtures: list[dict]) -> list[TestCaseResult]:
    """Requirement 3's "running isolated test cases before submitting
    back to the HITL pipeline": each fixture is
    `{"description": str, "input": {"entity_type":..., "facts": {...}},
    "expect_allow": bool}`. Uses the REAL, dependency-free
    `app.backtest.jsonlogic_evaluator` -- the same evaluator
    app.backtest uses to replay historical transactions -- so a passing
    result here means the exact evaluation OPA would perform also
    passed, not just that some standalone check did.

    A structurally invalid AST fails ALL fixtures immediately (there is
    no point evaluating a tree that can't even validate), each with the
    validator's own error message so the repair agent's next attempt
    knows exactly what was wrong -- Requirement 1's "syntax failures"
    class, at the JSON-Logic layer.
    """
    try:
        validate_json_logic_ast(json_logic)
    except Exception as exc:  # noqa: BLE001 - MalformedASTError or any validator exception, surfaced per-fixture below
        return [
            TestCaseResult(description=f["description"], passed=False, detail=f"AST failed structural validation: {exc}")
            for f in test_fixtures
        ]

    results: list[TestCaseResult] = []
    for fixture in test_fixtures:
        try:
            satisfied = bool(evaluate_jsonlogic(json_logic, fixture["input"]))
            expected = bool(fixture["expect_allow"])
            if satisfied == expected:
                results.append(TestCaseResult(description=fixture["description"], passed=True))
            else:
                results.append(
                    TestCaseResult(
                        description=fixture["description"],
                        passed=False,
                        detail=f"Expected allow={expected}, evaluator returned allow={satisfied} for input={fixture['input']!r}.",
                    )
                )
        except (UnsupportedJsonLogicNodeError, MissingFactError) as exc:
            # Requirement 1's "runtime evaluation crash": the AST is
            # structurally valid JSON-Logic but references a node shape
            # or fact path this test fixture's input can't satisfy.
            results.append(TestCaseResult(description=fixture["description"], passed=False, detail=f"Runtime evaluation crash ({type(exc).__name__}): {exc}"))
        except Exception as exc:  # noqa: BLE001 - any other evaluator crash is still a failed test case, not a loop-crashing exception
            results.append(TestCaseResult(description=fixture["description"], passed=False, detail=f"Unexpected evaluation error ({type(exc).__name__}): {exc}"))

    return results
