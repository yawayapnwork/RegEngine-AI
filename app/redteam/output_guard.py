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


def _normalize_text(s: str) -> str:
    import unicodedata

    return " ".join(unicodedata.normalize("NFKC", s).lower().split())


def validate_source_grounding(
    extracted: Any,
    source_text: str,
) -> list[Any]:
    """Validates that every claim (NumericalThreshold, TargetEntity, QualitativeDirective)
    in an ExtractedComplianceRule is verifiably grounded with exact verbatim source evidence
    in the source clause text, and detects any adversarial injection payload masquerading as
    a threshold or obligation."""
    from app.agents.schemas import AuditFinding, FindingType, Severity
    from app.redteam.defense import detect_injection_patterns

    findings: list[AuditFinding] = []
    norm_source = _normalize_text(source_text or "")

    # 1. Validate NumericalThresholds
    for i, t in enumerate(getattr(extracted, "deterministic_logic", [])):
        quote = t.verbatim_evidence or ""
        norm_quote = _normalize_text(quote)

        # 1a. Missing evidence
        if not norm_quote:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.UNSUPPORTED_CLAIM,
                    severity=Severity.BLOCKER,
                    field_path=f"deterministic_logic[{i}].verbatim_evidence",
                    description=f"Numerical threshold on '{t.metric}' is missing verbatim source evidence.",
                )
            )
            continue

        # 1b. Check for injection payloads inside threshold evidence or metric
        matched_inj = detect_injection_patterns(quote) + detect_injection_patterns(t.metric or "")
        if matched_inj or "[REDACTED-POSSIBLE-INJECTION" in quote:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.UNSUPPORTED_CLAIM,
                    severity=Severity.BLOCKER,
                    field_path=f"deterministic_logic[{i}].verbatim_evidence",
                    description=(
                        f"Numerical threshold on '{t.metric}' incorporates injected instruction / adversarial payload: "
                        f"{matched_inj or 'redacted injection payload'}"
                    ),
                    source_excerpt=quote[:200],
                )
            )
            continue

        # 1c. Evidence must exist in source text
        if norm_quote not in norm_source:
            import re
            from difflib import SequenceMatcher

            quote_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", norm_quote))
            source_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", norm_source))
            has_unsupported_numbers = bool(quote_numbers - source_numbers)

            matcher = SequenceMatcher(None, norm_quote, norm_source)
            match_block = matcher.find_longest_match(0, len(norm_quote), 0, len(norm_source))
            ratio = match_block.size / max(len(norm_quote), 1)
            if has_unsupported_numbers or ratio < 0.90:
                findings.append(
                    AuditFinding(
                        finding_type=FindingType.HALLUCINATED_THRESHOLD,
                        severity=Severity.BLOCKER,
                        field_path=f"deterministic_logic[{i}].verbatim_evidence",
                        description=(
                            f"Verbatim evidence '{quote[:100]}' for threshold '{t.metric}' does not exist in source clause text."
                        ),
                        source_excerpt=source_text[:200] if source_text else None,
                    )
                )
                continue

        # 1d. Numerical value must be grounded as an exact numeric token in source text AND verbatim evidence
        import re
        val_str = str(t.value)
        int_val_str = str(int(t.value)) if isinstance(t.value, (int, float)) and float(t.value).is_integer() else val_str
        quote_tokens = set(re.findall(r"\b\d+(?:\.\d+)?\b", quote))
        source_tokens = set(re.findall(r"\b\d+(?:\.\d+)?\b", source_text))

        val_in_quote = (val_str in quote_tokens or int_val_str in quote_tokens)
        val_in_source = (val_str in source_tokens or int_val_str in source_tokens)

        if not val_in_source or not val_in_quote:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.HALLUCINATED_THRESHOLD,
                    severity=Severity.BLOCKER,
                    field_path=f"deterministic_logic[{i}].value",
                    description=(
                        f"Threshold value {t.value} is not substantiated by verbatim evidence '{quote}' or source text."
                    ),
                    source_excerpt=quote,
                )
            )

    # 2. Validate TargetEntities
    for j, ent in enumerate(getattr(extracted, "target_entities", [])):
        quote = ent.verbatim_evidence or ""
        norm_quote = _normalize_text(quote)
        if not norm_quote:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.UNSUPPORTED_CLAIM,
                    severity=Severity.BLOCKER,
                    field_path=f"target_entities[{j}].verbatim_evidence",
                    description=f"Target entity '{ent.normalized_entity}' is missing verbatim source evidence.",
                )
            )
        elif norm_quote not in norm_source:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.HALLUCINATED_ENTITY,
                    severity=Severity.BLOCKER,
                    field_path=f"target_entities[{j}].verbatim_evidence",
                    description=f"Target entity evidence '{quote}' not found in source text.",
                    source_excerpt=source_text[:200] if source_text else None,
                )
            )

    # 3. Validate QualitativeDirectives
    for k, qd in enumerate(getattr(extracted, "qualitative_directives", [])):
        quote = qd.verbatim_evidence or ""
        norm_quote = _normalize_text(quote)
        if not norm_quote or norm_quote not in norm_source:
            findings.append(
                AuditFinding(
                    finding_type=FindingType.UNSUPPORTED_CLAIM,
                    severity=Severity.BLOCKER,
                    field_path=f"qualitative_directives[{k}].verbatim_evidence",
                    description=f"Qualitative directive evidence '{quote[:100]}' not found in source text.",
                )
            )

    return findings


def guard_and_validate_extraction(
    extracted: Any,
    audit: Any,
    chunk: Any,
    settings: Settings | None = None,
) -> tuple[Any, Any]:
    """Connects output guard protection and source grounding verification to an
    extracted compliance rule and its audit artifact.

    Enforces:
    1. Output leakage detection via Guardrails AI check_extraction_output().
    2. Redaction of any leaked instruction phrases in free-text fields.
    3. Detection and rejection of suspicious instruction-following output
       (e.g., attempt to approve policy, bypass HITL, disable verification, set margin to 0).
    4. Strict source grounding: every threshold and entity must be verified against chunk.text.
    5. Rejection: any blocker or ungrounded claim demotes audit.verdict to REJECTED with fidelity_score=0.0.
    """
    from app.agents.schemas import AuditFinding, AuditVerdict, FindingType, Severity
    from app.config import get_settings

    settings = settings or get_settings()

    # 1. Output guard check on free-text fields
    notes = getattr(extracted, "extraction_notes", None) or ""
    spans = getattr(extracted, "ambiguous_spans", []) or []

    guard_res = check_extraction_output(notes, spans, settings)
    if not guard_res.validation_passed:
        # Sanitize / redact free text
        guarded_notes = guard_res.guarded_output.get("extraction_notes", notes)
        guarded_spans_joined = guard_res.guarded_output.get("ambiguous_spans_joined", "")
        extracted.extraction_notes = guarded_notes
        extracted.ambiguous_spans = (
            [s.strip() for s in guarded_spans_joined.split(" | ") if s.strip()]
            if guarded_spans_joined
            else []
        )
        audit.findings.append(
            AuditFinding(
                finding_type=FindingType.UNSUPPORTED_CLAIM,
                severity=Severity.BLOCKER,
                field_path="extraction_notes",
                description=(
                    "Output guard detected instruction-leakage or prompt injection in extracted output. "
                    "Rule rejected to prevent execution of unauthorized commands."
                ),
            )
        )
        audit.verdict = AuditVerdict.REJECTED
        audit.fidelity_score = 0.0

    # 2. Validate source grounding
    grounding_findings = validate_source_grounding(extracted, getattr(chunk, "text", "") or "")
    if grounding_findings:
        audit.findings.extend(grounding_findings)
        has_blocker = any(f.severity == Severity.BLOCKER for f in grounding_findings)
        if has_blocker:
            audit.verdict = AuditVerdict.REJECTED
            audit.fidelity_score = 0.0
        audit.unverified_quote_count += len(grounding_findings)

    return extracted, audit
