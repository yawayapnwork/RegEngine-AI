"""Provider abstraction for the RegEngine LLM extraction/audit pipeline.

Supports:
- LLM_PROVIDER=offline (deterministic/local rule extractor for POC & demos)
- LLM_PROVIDER=openai
- LLM_PROVIDER=anthropic
- LLM_PROVIDER=huggingface

Guarantees:
1. Valid external providers are used when configured with valid credentials.
2. Offline mode operates deterministically without external LLMs, clearly identifying its output.
3. If an external provider fails (missing key, timeout, provider error, malformed output):
   - Never silently generates an unsafe policy.
   - Returns a controlled REVIEW_REQUIRED state or raises a structured exception.
4. Fallback to an automatically deployable rule occurs ONLY if the deterministic extractor can
   prove the rule from exact verbatim quotes in the source text.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from app.agents.schemas import (
    AuditedComplianceRule,
    AuditFinding,
    AuditVerdict,
    ComparisonOperator,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
    FindingType,
    NumericalThreshold,
    ObligationType,
    QualitativeDirective,
    Severity,
    TargetEntity,
)
from app.config import Settings, get_settings
from app.models import ClauseChunk
from app.regulatory.facts import get_canonical_fact, resolve_canonical_fact
from app.regulatory.taxonomy import resolve_domain

logger = logging.getLogger(__name__)


class LLMProviderType(str, Enum):
    OFFLINE = "offline"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    HUGGINGFACE = "huggingface"


# ---------------------------------------------------------------------------
# Provider Exceptions
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base exception for LLM provider errors."""
    pass


class MissingAPIKeyError(ProviderError):
    """Raised when an external provider is selected without its required API key."""
    pass


class ProviderTimeoutError(ProviderError):
    """Raised when an LLM provider call times out."""
    pass


class ProviderExecutionError(ProviderError):
    """Raised when an external LLM provider encounters an execution, connection, or HTTP error."""
    pass


class MalformedOutputError(ProviderError):
    """Raised when an LLM produces malformed or unparseable output."""
    pass


# ---------------------------------------------------------------------------
# Controlled Failure Fallback
# ---------------------------------------------------------------------------


def _controlled_failure_rule(
    chunk: ClauseChunk,
    provider_type: LLMProviderType,
    error: Exception,
) -> AuditedComplianceRule:
    """Builds a controlled non-executable AuditedComplianceRule in REJECTED state,
    ensuring that a provider failure never produces an automatically deployable policy
    and always routes to REVIEW_REQUIRED.
    """
    rule_id = f"{chunk.sha256}:{chunk.clause_number or 'unscoped'}"
    rule = ExtractedComplianceRule(
        rule_id=rule_id,
        source_chunk_id=chunk.chunk_id,
        source_sha256=chunk.sha256,
        circular_number=chunk.circular_number,
        clause_number=chunk.clause_number,
        section_path=chunk.section_path,
        target_entities=[],
        deterministic_logic=[],
        obligation_type=ObligationType.MANDATORY,
        extraction_confidence=0.0,
        extraction_notes=(
            f"Provider '{provider_type.value}' failed ({type(error).__name__}: {error}). "
            "Deterministic proof was not possible from source text. Routed to human review."
        ),
    )
    audit = ComplianceRuleAudit(
        rule_id=rule_id,
        verdict=AuditVerdict.REJECTED,
        fidelity_score=0.0,
        findings=[
            AuditFinding(
                finding_type=FindingType.UNSUPPORTED_CLAIM,
                severity=Severity.BLOCKER,
                field_path="deterministic_logic",
                description=(
                    f"LLM Provider '{provider_type.value}' failed ({type(error).__name__}: {error}). "
                    "Cannot verify rule without human review."
                ),
                source_excerpt=chunk.text[:200] if chunk.text else None,
            )
        ],
        verified_quote_count=0,
        unverified_quote_count=1,
    )
    return AuditedComplianceRule(rule=rule, audit=audit, revision_round=0)


# ---------------------------------------------------------------------------
# Base Provider
# ---------------------------------------------------------------------------


class BaseLLMProvider(ABC):
    provider_type: LLMProviderType

    @abstractmethod
    def get_llm(
        self,
        settings: Settings,
        *,
        temperature: float = 0.0,
        model_override: str | None = None,
    ) -> Any:
        """Returns the LLM engine for CrewAI agents."""
        ...

    @abstractmethod
    def extract_and_audit(
        self,
        chunk: ClauseChunk,
        sibling_chunks: list[dict],
        settings: Settings,
        *,
        raise_on_failure: bool = False,
    ) -> AuditedComplianceRule:
        """Extract and audit a clause chunk."""
        ...


# ---------------------------------------------------------------------------
# Offline / Deterministic Provider
# ---------------------------------------------------------------------------


class OfflineLLMProvider(BaseLLMProvider):
    """Deterministic, local rule extractor for POC, demo, and test environments.

    Does NOT pretend to be an LLM. Clearly identifies its output as deterministic/demo
    extraction and enforces that only provable rules with verified source quotes are produced.
    """

    provider_type = LLMProviderType.OFFLINE

    def get_llm(
        self,
        settings: Settings,
        *,
        temperature: float = 0.0,
        model_override: str | None = None,
    ) -> Any:
        return None

    def extract_and_audit(
        self,
        chunk: ClauseChunk,
        sibling_chunks: list[dict],
        settings: Settings,
        *,
        raise_on_failure: bool = False,
    ) -> AuditedComplianceRule:
        from app.redteam.defense import sanitize_metadata_field, sanitize_source_text
        from app.redteam.output_guard import guard_and_validate_extraction

        raw_text = chunk.text or ""
        sanitization = sanitize_source_text(raw_text)
        text = sanitization.cleaned_text
        lower_text = text.lower()

        clean_circular = sanitize_metadata_field(chunk.circular_number)
        clean_clause = sanitize_metadata_field(chunk.clause_number)
        rule_id = f"{chunk.sha256}:{clean_clause or 'unscoped'}"

        thresholds: list[NumericalThreshold] = []
        target_entities: list[TargetEntity] = []

        # 1. Detect target entities (e.g. Stockbroker / Clearing Member)
        entity_raw = "stock broker"
        if "stock broker" in lower_text or "stockbroker" in lower_text or "broker" in lower_text:
            m_ent = re.search(r"\b(stock\s*brokers?|brokers?)\b", text, re.IGNORECASE)
            entity_raw = m_ent.group(1) if m_ent else "stock broker"
            target_entities.append(
                TargetEntity(
                    raw_text=entity_raw,
                    normalized_entity="Stockbroker",
                    verbatim_evidence=entity_raw,
                )
            )

        # 2. Pattern A: Upfront Margin Percentage
        # Matches e.g. "minimum upfront margin of 20%", "margin of not less than 20%"
        m_margin = re.search(
            r"((?:minimum\s+|mandatory\s+|upfront\s+)?margin(?:\s+of\s+not\s+less\s+than|\s+requirement|\s+collection)?\s*(?:of\s+)?(?:at\s+least\s+)?(\d+(?:\.\d+)?)\s*(%|percent))",
            text,
            re.IGNORECASE,
        )
        if m_margin:
            full_span, val_str, unit_str = m_margin.group(1), m_margin.group(2), m_margin.group(3)
            val = float(val_str)
            thresholds.append(
                NumericalThreshold(
                    metric="Upfront Margin",
                    canonical_fact="upfront_margin_pct",
                    operator=ComparisonOperator.GTE,
                    value=val,
                    unit="%",
                    applies_to="Stockbroker" if target_entities else None,
                    verbatim_evidence=full_span,
                )
            )
        elif "margin" in lower_text and ("%" in text or "percent" in lower_text):
            m_pct = re.search(r"(\d+(?:\.\d+)?)\s*(%|percent)", text, re.IGNORECASE)
            if m_pct:
                val = float(m_pct.group(1))
                # Find the sentence containing this percentage
                sentences = re.split(r"[.\n]", text)
                quote = next((s.strip() for s in sentences if m_pct.group(0) in s), m_pct.group(0))
                thresholds.append(
                    NumericalThreshold(
                        metric="Upfront Margin",
                        canonical_fact="upfront_margin_pct",
                        operator=ComparisonOperator.GTE,
                        value=val,
                        unit="%",
                        applies_to="Stockbroker" if target_entities else None,
                        verbatim_evidence=quote,
                    )
                )

        # 3. Pattern B: Client Collateral
        m_collateral = re.search(
            r"((?:client\s+)?collateral(?:\s+requirement)?\s*(?:of\s+at\s+least|not\s+less\s+than|of)\s*(\d+(?:\.\d+)?)\s*(inr(?:\s+crore|\s+lakh)?|crore|lakh|rs\.?))",
            text,
            re.IGNORECASE,
        )
        if m_collateral:
            full_span, val_str, unit_str = m_collateral.group(1), m_collateral.group(2), m_collateral.group(3)
            val = float(val_str)
            thresholds.append(
                NumericalThreshold(
                    metric="Client Collateral",
                    canonical_fact="client_collateral",
                    operator=ComparisonOperator.GTE,
                    value=val,
                    unit=unit_str,
                    applies_to="Stockbroker" if target_entities else None,
                    verbatim_evidence=full_span,
                )
            )

        # 4. Pattern C: Peak Margin
        m_peak = re.search(
            r"(peak\s+margin\s*(?:requirement)?\s*(?:of\s+at\s+least|not\s+less\s+than|of)\s*(\d+(?:\.\d+)?)\s*(%|percent))",
            text,
            re.IGNORECASE,
        )
        if m_peak:
            full_span, val_str, unit_str = m_peak.group(1), m_peak.group(2), m_peak.group(3)
            val = float(val_str)
            thresholds.append(
                NumericalThreshold(
                    metric="Peak Margin",
                    canonical_fact="peak_margin",
                    operator=ComparisonOperator.GTE,
                    value=val,
                    unit="%",
                    applies_to="Stockbroker" if target_entities else None,
                    verbatim_evidence=full_span,
                )
            )

        # 5. Pattern D: Reporting Deadline
        m_deadline = re.search(
            r"((?:report|reporting|submission|filing)\s*(?:deadline|timeline)?\s*(?:within|is)\s*(\d+)\s*(days|hours|months|weeks))",
            text,
            re.IGNORECASE,
        )
        if m_deadline:
            full_span, val_str, unit_str = m_deadline.group(1), m_deadline.group(2), m_deadline.group(3)
            val = float(val_str)
            thresholds.append(
                NumericalThreshold(
                    metric="Reporting Deadline",
                    canonical_fact="reporting_deadline",
                    operator=ComparisonOperator.LTE,
                    value=val,
                    unit=unit_str,
                    applies_to=None,
                    verbatim_evidence=full_span,
                )
            )

        # If provable numeric thresholds were found and grounded in text:
        if thresholds:
            rule = ExtractedComplianceRule(
                rule_id=rule_id,
                source_chunk_id=chunk.chunk_id,
                source_sha256=chunk.sha256,
                circular_number=clean_circular,
                clause_number=clean_clause,
                section_path=chunk.section_path,
                regulator=chunk.regulator,
                regulatory_domain=resolve_domain(chunk.regulator, target_entities[0].normalized_entity if target_entities else None),
                target_entities=target_entities,
                deterministic_logic=thresholds,
                obligation_type=ObligationType.MANDATORY,
                extraction_confidence=1.0,
                extraction_notes="Deterministic rule extraction (offline demo mode, no external LLM used)",
            )
            audit = ComplianceRuleAudit(
                rule_id=rule_id,
                verdict=AuditVerdict.APPROVED,
                fidelity_score=1.0,
                findings=[],
                verified_quote_count=len(thresholds) + len(target_entities),
                unverified_quote_count=0,
            )
            rule, audit = guard_and_validate_extraction(rule, audit, chunk, settings)
            return AuditedComplianceRule(rule=rule, audit=audit, revision_round=0)

        # Unsupported rule: clause text does not contain a verifiable numeric obligation.
        # Mark REJECTED and route to REVIEW_REQUIRED rather than inventing arbitrary logic.
        rule = ExtractedComplianceRule(
            rule_id=rule_id,
            source_chunk_id=chunk.chunk_id,
            source_sha256=chunk.sha256,
            circular_number=clean_circular,
            clause_number=clean_clause,
            section_path=chunk.section_path,
            regulator=chunk.regulator,
            regulatory_domain=resolve_domain(chunk.regulator, None),
            target_entities=[],
            deterministic_logic=[],
            qualitative_directives=[
                QualitativeDirective(
                    directive_text=text[:200],
                    verbatim_evidence=text[:200],
                )
            ] if text else [],
            obligation_type=ObligationType.RECOMMENDED,
            extraction_confidence=0.0,
            extraction_notes="Offline extraction: clause text contains no verifiable deterministic numeric threshold.",
        )
        audit = ComplianceRuleAudit(
            rule_id=rule_id,
            verdict=AuditVerdict.REJECTED,
            fidelity_score=0.0,
            findings=[
                AuditFinding(
                    finding_type=FindingType.UNSUPPORTED_CLAIM,
                    severity=Severity.BLOCKER,
                    field_path="deterministic_logic",
                    description=(
                        "Unsupported rule: source clause text does not contain a verifiable numeric obligation "
                        "in offline mode. Human review required."
                    ),
                    source_excerpt=text[:200] if text else None,
                )
            ],
            verified_quote_count=0,
            unverified_quote_count=1,
        )
        rule, audit = guard_and_validate_extraction(rule, audit, chunk, settings)
        return AuditedComplianceRule(rule=rule, audit=audit, revision_round=0)


# ---------------------------------------------------------------------------
# External Providers (OpenAI, Anthropic, HuggingFace)
# ---------------------------------------------------------------------------


class ExternalLLMProvider(BaseLLMProvider):
    """Base class for external LLM API providers with failure handling."""

    @abstractmethod
    def validate_api_key(self, settings: Settings) -> None:
        """Validates that the provider's API key is present."""
        ...

    def extract_and_audit(
        self,
        chunk: ClauseChunk,
        sibling_chunks: list[dict],
        settings: Settings,
        *,
        raise_on_failure: bool = False,
    ) -> AuditedComplianceRule:
        """Executes extraction and auditing via CrewAI with the configured external LLM.

        If the provider fails (missing key, timeout, error, malformed output):
        - If raise_on_failure is True, raises the structured exception.
        - Otherwise, falls back to deterministic extraction ONLY if proven from source text.
        - If not provable, returns a controlled REVIEW_REQUIRED state.
        """
        try:
            self.validate_api_key(settings)
            from app.agents.crew import _run_crew_loop
            return _run_crew_loop(chunk, sibling_chunks, settings, provider=self)
        except Exception as exc:
            if raise_on_failure:
                raise
            logger.warning(
                "External LLM provider '%s' failed: %s: %s",
                self.provider_type.value,
                type(exc).__name__,
                exc,
            )
            # Requirement 5: Never fall back from a failed LLM directly to an automatically
            # deployable rule unless the deterministic extractor can prove the rule from the source text.
            offline = OfflineLLMProvider()
            candidate = offline.extract_and_audit(chunk, sibling_chunks, settings)
            if candidate.audit.verdict == AuditVerdict.APPROVED and candidate.rule.deterministic_logic:
                candidate.rule.extraction_notes = (
                    f"Deterministic extraction following {self.provider_type.value} failure "
                    f"({type(exc).__name__}: {exc}). Rule verified directly against source text."
                )
                return candidate

            # Not provable from text: return controlled REVIEW_REQUIRED
            return _controlled_failure_rule(chunk, self.provider_type, exc)


class OpenAIProvider(ExternalLLMProvider):
    provider_type = LLMProviderType.OPENAI

    def validate_api_key(self, settings: Settings) -> None:
        if not settings.openai_api_key or not settings.openai_api_key.strip():
            raise MissingAPIKeyError("OpenAI API key is missing. Set OPENAI_API_KEY environment variable.")

    def get_llm(
        self,
        settings: Settings,
        *,
        temperature: float = 0.0,
        model_override: str | None = None,
    ) -> Any:
        self.validate_api_key(settings)
        from crewai import LLM
        model = model_override or f"openai/{settings.openai_model_id}"
        return LLM(
            model=model,
            api_key=settings.openai_api_key,
            temperature=temperature,
            max_tokens=4096,
        )


class AnthropicProvider(ExternalLLMProvider):
    provider_type = LLMProviderType.ANTHROPIC

    def validate_api_key(self, settings: Settings) -> None:
        if not settings.anthropic_api_key or not settings.anthropic_api_key.strip():
            raise MissingAPIKeyError("Anthropic API key is missing. Set ANTHROPIC_API_KEY environment variable.")

    def get_llm(
        self,
        settings: Settings,
        *,
        temperature: float = 0.0,
        model_override: str | None = None,
    ) -> Any:
        self.validate_api_key(settings)
        from crewai import LLM
        model = model_override or f"anthropic/{settings.anthropic_model_id}"
        return LLM(
            model=model,
            api_key=settings.anthropic_api_key,
            temperature=temperature,
            max_tokens=4096,
        )


class HuggingFaceProvider(ExternalLLMProvider):
    provider_type = LLMProviderType.HUGGINGFACE

    def validate_api_key(self, settings: Settings) -> None:
        if not settings.hf_api_token or not settings.hf_api_token.strip():
            raise MissingAPIKeyError("Hugging Face token is missing. Set HF_TOKEN environment variable.")

    def get_llm(
        self,
        settings: Settings,
        *,
        temperature: float = 0.0,
        model_override: str | None = None,
    ) -> Any:
        self.validate_api_key(settings)
        from crewai import LLM
        model = model_override or f"huggingface/{settings.hf_model_id}"
        return LLM(
            model=model,
            api_key=settings.hf_api_token,
            temperature=temperature,
            max_tokens=4096,
        )


# ---------------------------------------------------------------------------
# Provider Factory
# ---------------------------------------------------------------------------


def get_provider(settings: Settings | None = None) -> BaseLLMProvider:
    """Factory resolving the configured LLM provider from settings.

    Supported values for LLM_PROVIDER:
    - 'offline' (default/demo)
    - 'openai'
    - 'anthropic'
    - 'huggingface'
    """
    settings = settings or get_settings()
    raw = (settings.llm_provider or "").strip().lower()

    if raw == "openai":
        return OpenAIProvider()
    if raw == "anthropic":
        return AnthropicProvider()
    if raw == "huggingface":
        return HuggingFaceProvider()
    if raw == "offline":
        return OfflineLLMProvider()

    # Fallback heuristics if unspecified or non-standard
    if settings.openai_api_key:
        return OpenAIProvider()
    if settings.anthropic_api_key:
        return AnthropicProvider()
    if settings.hf_api_token:
        return HuggingFaceProvider()

    # Default to offline mode
    return OfflineLLMProvider()
