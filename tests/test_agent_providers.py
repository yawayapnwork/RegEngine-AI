"""Tests for the LLM Provider abstraction in RegEngine AI.

Covers:
* Offline mode (deterministic/demo extraction)
* Configured external providers (OpenAI, Anthropic, HuggingFace)
* Missing API key handling
* Provider timeout handling
* Provider execution error handling
* Malformed LLM output handling
* Unsupported rule handling (refusal to invent rules)
* Safety guarantee: never falling back to an unsafe policy unless proven from text
"""
from __future__ import annotations

import pytest

from app.agents.providers import (
    AnthropicProvider,
    HuggingFaceProvider,
    MalformedOutputError,
    MissingAPIKeyError,
    OfflineLLMProvider,
    OpenAIProvider,
    ProviderExecutionError,
    ProviderTimeoutError,
    get_provider,
)
from app.agents.schemas import (
    AuditVerdict,
    ExtractedComplianceRule,
    ObligationType,
)
from app.compiler.hitl import collect_hitl_flags, has_blocking_flags
from app.compiler.pipeline import compile_audited_rule
from app.config import Settings
from app.models import ClauseChunk
from app.regulatory.taxonomy import Regulator


@pytest.fixture
def sample_margin_chunk() -> ClauseChunk:
    return ClauseChunk(
        chunk_id="chunk_margin_01",
        text="Clause 2.1: Stock brokers shall collect a minimum upfront margin of 20% from all clients.",
        circular_number="SEBI/HO/MRD/2026/001",
        clause_number="2.1",
        section_path=["2", "2.1"],
        sha256="abc123sha256margin000000000000000000000000000000000000000000000001",
        regulator=Regulator.SEBI,
    )


@pytest.fixture
def sample_unsupported_chunk() -> ClauseChunk:
    return ClauseChunk(
        chunk_id="chunk_narrative_02",
        text="Clause 5.4: The compliance officer shall maintain adequate oversight over administrative functions.",
        circular_number="SEBI/HO/MRD/2026/001",
        clause_number="5.4",
        section_path=["5", "5.4"],
        sha256="abc123sha256narrative00000000000000000000000000000000000000000000002",
        regulator=Regulator.SEBI,
    )


# ---------------------------------------------------------------------------
# 1. Provider Resolution & Configuration
# ---------------------------------------------------------------------------


def test_provider_factory_resolution() -> None:
    # 1. Explicit offline
    s_offline = Settings(llm_provider="offline")
    p_offline = get_provider(s_offline)
    assert isinstance(p_offline, OfflineLLMProvider)

    # 2. Explicit OpenAI
    s_openai = Settings(llm_provider="openai", openai_api_key="sk-test-key")
    p_openai = get_provider(s_openai)
    assert isinstance(p_openai, OpenAIProvider)

    # 3. Explicit Anthropic
    s_anthropic = Settings(llm_provider="anthropic", anthropic_api_key="sk-ant-test")
    p_anthropic = get_provider(s_anthropic)
    assert isinstance(p_anthropic, AnthropicProvider)

    # 4. Explicit HuggingFace
    s_hf = Settings(llm_provider="huggingface", hf_api_token="hf_test_token")
    p_hf = get_provider(s_hf)
    assert isinstance(p_hf, HuggingFaceProvider)

    # 5. Default when no keys configured is offline
    s_default = Settings(llm_provider="offline", openai_api_key=None, hf_api_token=None)
    assert isinstance(get_provider(s_default), OfflineLLMProvider)


# ---------------------------------------------------------------------------
# 2. Offline Mode (Deterministic Demo Extraction)
# ---------------------------------------------------------------------------


def test_offline_mode_extraction_and_identity(sample_margin_chunk: ClauseChunk) -> None:
    settings = Settings(llm_provider="offline")
    provider = get_provider(settings)
    assert isinstance(provider, OfflineLLMProvider)

    audited = provider.extract_and_audit(sample_margin_chunk, [], settings)

    # 1. Output must NOT pretend to be an LLM
    assert "deterministic" in audited.rule.extraction_notes.lower()
    assert "no external llm used" in audited.rule.extraction_notes.lower()

    # 2. Proved rule from source text
    assert len(audited.rule.deterministic_logic) == 1
    thresh = audited.rule.deterministic_logic[0]
    assert thresh.canonical_fact == "upfront_margin_pct"
    assert thresh.value == 20.0
    assert thresh.unit == "%"
    # Grounded verbatim evidence
    assert thresh.verbatim_evidence in sample_margin_chunk.text

    # 3. Audit approved
    assert audited.audit.verdict == AuditVerdict.APPROVED
    assert audited.audit.verified_quote_count >= 1
    assert audited.audit.unverified_quote_count == 0

    # 4. Can be compiled to Rego
    compilation = compile_audited_rule(audited)
    assert compilation.compiled is True
    assert "input.facts.upfront_margin_pct >= 20" in compilation.rego.rego_code


# ---------------------------------------------------------------------------
# 3. Unsupported Rule Handling (Refusal to Invent Rules)
# ---------------------------------------------------------------------------


def test_offline_mode_unsupported_rule_routes_to_review(sample_unsupported_chunk: ClauseChunk) -> None:
    settings = Settings(llm_provider="offline")
    provider = get_provider(settings)

    audited = provider.extract_and_audit(sample_unsupported_chunk, [], settings)

    # Must NOT invent a threshold
    assert len(audited.rule.deterministic_logic) == 0

    # Must be marked REJECTED / REVIEW_REQUIRED
    assert audited.audit.verdict == AuditVerdict.REJECTED
    assert len(audited.audit.findings) >= 1
    assert audited.audit.findings[0].finding_type.value == "unsupported_claim"

    # Compiler refuses compilation and flags for human review
    flags = collect_hitl_flags(audited)
    assert has_blocking_flags(flags) is True

    compilation = compile_audited_rule(audited)
    assert compilation.compiled is False
    assert compilation.rego is None


# ---------------------------------------------------------------------------
# 4. Missing API Key Handling
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_when_requested(sample_margin_chunk: ClauseChunk) -> None:
    settings = Settings(llm_provider="openai", openai_api_key=None)
    provider = OpenAIProvider()

    with pytest.raises(MissingAPIKeyError):
        provider.extract_and_audit(sample_margin_chunk, [], settings, raise_on_failure=True)


def test_missing_api_key_safe_fallback_when_unprovable(sample_unsupported_chunk: ClauseChunk) -> None:
    settings = Settings(llm_provider="openai", openai_api_key=None)
    provider = OpenAIProvider()

    # When unprovable, must return controlled REVIEW_REQUIRED state rather than inventing
    audited = provider.extract_and_audit(sample_unsupported_chunk, [], settings, raise_on_failure=False)
    assert audited.audit.verdict == AuditVerdict.REJECTED
    assert len(audited.rule.deterministic_logic) == 0
    assert "failed" in audited.rule.extraction_notes.lower()

    compilation = compile_audited_rule(audited)
    assert compilation.compiled is False


# ---------------------------------------------------------------------------
# 5. Provider Timeout Handling
# ---------------------------------------------------------------------------


def test_provider_timeout_handling(monkeypatch: pytest.MonkeyPatch, sample_margin_chunk: ClauseChunk) -> None:
    settings = Settings(llm_provider="anthropic", anthropic_api_key="sk-ant-test")
    provider = AnthropicProvider()

    def _mock_failing_crew(*args, **kwargs):
        raise TimeoutError("Anthropic API request timed out after 60s")

    monkeypatch.setattr("app.agents.crew._run_crew_loop", _mock_failing_crew)

    # 1. When raise_on_failure=True, raises structured error
    with pytest.raises(TimeoutError):
        provider.extract_and_audit(sample_margin_chunk, [], settings, raise_on_failure=True)

    # 2. When raise_on_failure=False on provable rule, falls back to proven rule with explicit note
    audited = provider.extract_and_audit(sample_margin_chunk, [], settings, raise_on_failure=False)
    assert "following anthropic failure" in audited.rule.extraction_notes.lower()
    assert audited.rule.deterministic_logic[0].canonical_fact == "upfront_margin_pct"


# ---------------------------------------------------------------------------
# 6. Provider Execution Error Handling
# ---------------------------------------------------------------------------


def test_provider_execution_error_handling(monkeypatch: pytest.MonkeyPatch, sample_unsupported_chunk: ClauseChunk) -> None:
    settings = Settings(llm_provider="huggingface", hf_api_token="hf_test_token")
    provider = HuggingFaceProvider()

    def _mock_500_error(*args, **kwargs):
        raise RuntimeError("HTTP 500: Hugging Face Inference server overloaded")

    monkeypatch.setattr("app.agents.crew._run_crew_loop", _mock_500_error)

    # When rule is unsupported in source text, provider error MUST NOT invent a rule
    audited = provider.extract_and_audit(sample_unsupported_chunk, [], settings, raise_on_failure=False)
    assert audited.audit.verdict == AuditVerdict.REJECTED
    assert len(audited.rule.deterministic_logic) == 0
    assert "failed" in audited.rule.extraction_notes.lower()

    # Must be marked review required
    flags = collect_hitl_flags(audited)
    assert has_blocking_flags(flags) is True


# ---------------------------------------------------------------------------
# 7. Malformed LLM Output Handling
# ---------------------------------------------------------------------------


def test_malformed_llm_output_handling(monkeypatch: pytest.MonkeyPatch, sample_unsupported_chunk: ClauseChunk) -> None:
    settings = Settings(llm_provider="openai", openai_api_key="sk-test")
    provider = OpenAIProvider()

    def _mock_malformed(*args, **kwargs):
        raise MalformedOutputError("LLM returned malformed JSON missing required fields")

    monkeypatch.setattr("app.agents.crew._run_crew_loop", _mock_malformed)

    # 1. Raises when requested
    with pytest.raises(MalformedOutputError):
        provider.extract_and_audit(sample_unsupported_chunk, [], settings, raise_on_failure=True)

    # 2. Returns controlled review-required rejection when handled
    audited = provider.extract_and_audit(sample_unsupported_chunk, [], settings, raise_on_failure=False)
    assert audited.audit.verdict == AuditVerdict.REJECTED
    assert "malformedoutputerror" in audited.rule.extraction_notes.lower()
    assert len(audited.rule.deterministic_logic) == 0


# ---------------------------------------------------------------------------
# 8. Demo Runs Without External API Key
# ---------------------------------------------------------------------------


def test_demo_runs_without_external_api_key(sample_margin_chunk: ClauseChunk) -> None:
    """Proves the end-to-end extraction and compilation works cleanly in offline mode
    without requiring any external LLM credentials or network calls."""
    settings = Settings(
        llm_provider="offline",
        hf_api_token=None,
        openai_api_key=None,
        anthropic_api_key=None,
    )

    provider = get_provider(settings)
    audited = provider.extract_and_audit(sample_margin_chunk, [], settings)

    assert audited.audit.verdict == AuditVerdict.APPROVED
    assert audited.rule.deterministic_logic[0].value == 20.0

    compilation = compile_audited_rule(audited)
    assert compilation.compiled is True
    assert compilation.rego is not None
    assert "input.facts.upfront_margin_pct >= 20" in compilation.rego.rego_code
