"""Requirement 2 -- Defense Middleware, the structured-output-enforcement
half, via Guardrails AI (`guardrails-ai`, verified installed and
exercised for real in this environment -- see
tests/test_redteam.py::TestOutputGuard).

SECURITY FINDING (read before using this module in any deployment
handling real, non-test SEBI document content): `guardrails-ai==0.10.2`
constructs a global OpenTelemetry `TracerProvider` with an
`OTLPSpanExporter` AT IMPORT TIME, which -- observed directly in this
environment -- attempts to export spans to a hardcoded external
endpoint (`https://<redacted>.execute-api.us-east-1.amazonaws.com/v1/traces`,
Guardrails AI's own hosted telemetry service) regardless of the
documented `guardrails.settings.settings.disable_tracing` flag (setting
it AFTER import had no effect in this environment's testing -- the
`TracerProvider` singleton is already constructed by then). The only
reliable mitigation found here is the OpenTelemetry SDK's own
spec-defined `OTEL_SDK_DISABLED=true` environment variable, which MUST
be set BEFORE `guardrails` is first imported anywhere in the process.
Every entrypoint in this module (`build_guard`, `check_extraction_output`)
sets that variable BEFORE its own deferred `import guardrails`, and
refuses to proceed (raises `OutputGuardTelemetryError`) if it detects
`guardrails` was already imported elsewhere in the process without that
variable set -- a
compliance system must never risk exporting confidential regulatory
document content (even as span attributes/metadata) to an external,
unvetted third-party endpoint. `settings.redteam_disable_output_guard_telemetry`
(default True) controls whether this module enforces this at all;
leave it True unless you have independently verified your guardrails-ai
version/configuration does not phone home.
"""
from __future__ import annotations

import os
import sys

from app.config import Settings


class OutputGuardTelemetryError(RuntimeError):
    pass


def _ensure_telemetry_disabled(settings: Settings) -> None:
    if not settings.redteam_disable_output_guard_telemetry:
        return
    if "guardrails" in sys.modules and os.environ.get("OTEL_SDK_DISABLED") != "true":
        raise OutputGuardTelemetryError(
            "'guardrails' was already imported elsewhere in this process WITHOUT OTEL_SDK_DISABLED=true set first -- "
            "its telemetry TracerProvider (see this module's docstring) is likely already active and cannot be "
            "retroactively disabled. Set OTEL_SDK_DISABLED=true before any 'import guardrails' in this process, or "
            "import app.redteam.output_guard before anything else that might import guardrails."
        )
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")


import re
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

logger_name = __name__


class InjectionLeakageResult(BaseModel):
    field_path: str
    passed: bool
    detail: str | None = None
    original_value: str
    guarded_value: str


class OutputGuardResult(BaseModel):
    validation_passed: bool
    findings: list[InjectionLeakageResult]
    guarded_output: dict[str, Any]


@lru_cache(maxsize=1)
def _build_guard():
    """Guardrails AI's `Guard`/`Validator` objects are expensive enough
    (and stateless enough, given this module's fixed field list) to
    build once per process and reuse. `build_guard` (below) always
    calls `_ensure_telemetry_disabled` first, on every call, regardless
    of this cache -- so a caller cannot bypass the telemetry check by
    hitting a warm cache."""
    from guardrails import Guard  # deferred heavy import -- see _ensure_telemetry_disabled above, which MUST run first
    from guardrails.validators import FailResult, PassResult, Validator, register_validator

    from app.redteam.defense import detect_injection_patterns

    @register_validator(name="regengine/no-injected-instruction-leakage", data_type="string")
    class NoInjectedInstructionLeakage(Validator):
        def validate(self, value, metadata):  # noqa: ANN001 - guardrails' own base signature
            # Reuses app.redteam.defense's EXACT pattern table (see
            # detect_injection_patterns' docstring on why this must not
            # be a second, independently-maintained list).
            hits = detect_injection_patterns(value or "")
            if hits:
                return FailResult(
                    error_message=f"Output field contains injected-instruction leakage: {hits!r}",
                    fix_value=f"[REDACTED BY OUTPUT GUARD: possible prompt-injection leakage matching {hits!r}]",
                )
            return PassResult()

    class GuardedTextFields(BaseModel):
        extraction_notes: str = ""
        ambiguous_spans_joined: str = ""  # ExtractedComplianceRule.ambiguous_spans, pre-joined by the caller (see check_extraction_output) -- Guardrails validates scalar string fields, not list-of-string fields, cleanly in this version

    guard = Guard.for_pydantic(GuardedTextFields)
    guard.use(NoInjectedInstructionLeakage(on_fail="fix"), on="$.extraction_notes")
    guard.use(NoInjectedInstructionLeakage(on_fail="fix"), on="$.ambiguous_spans_joined")
    return guard


def build_guard(settings: Settings):
    _ensure_telemetry_disabled(settings)
    return _build_guard()


def check_extraction_output(extraction_notes: str | None, ambiguous_spans: list[str] | None, settings: Settings) -> OutputGuardResult:
    """Requirement 2's structured-output-enforcement guard, applied to
    the two free-text fields on `app.agents.schemas.ExtractedComplianceRule`
    an injected instruction could plausibly leak into or be echoed back
    through (every OTHER field is already schema-typed/enum-constrained
    by Pydantic via CrewAI's `output_pydantic=ExtractedComplianceRule`,
    which is itself a real structured-output enforcement layer this
    module adds a SECOND, injection-specific pass on top of, not a
    replacement for)."""
    guard = build_guard(settings)
    joined_spans = " | ".join(ambiguous_spans or [])

    import json

    outcome = guard.parse(json.dumps({"extraction_notes": extraction_notes or "", "ambiguous_spans_joined": joined_spans}))
    guarded = outcome.validated_output or {}

    findings = [
        InjectionLeakageResult(
            field_path="extraction_notes",
            passed=guarded.get("extraction_notes") == (extraction_notes or ""),
            original_value=extraction_notes or "",
            guarded_value=guarded.get("extraction_notes", ""),
        ),
        InjectionLeakageResult(
            field_path="ambiguous_spans",
            passed=guarded.get("ambiguous_spans_joined") == joined_spans,
            original_value=joined_spans,
            guarded_value=guarded.get("ambiguous_spans_joined", ""),
        ),
    ]
    for f in findings:
        if not f.passed:
            f.detail = "Injected-instruction leakage detected and redacted -- see guarded_value."

    return OutputGuardResult(validation_passed=all(f.passed for f in findings), findings=findings, guarded_output=guarded)
