"""Typed request and response models for the regengine-python SDK.

These mirror the server-side Pydantic models but are kept intentionally
independent — the SDK must not import from the server package.  The shapes
are kept in sync with the API contract; breaking changes to the server API
will surface as validation errors on these models, which is the right
failure mode for a versioned client library.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared enumerations
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    FLAGGED = "flagged"


class EvaluationOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    HITL_REVIEW = "HITL_REVIEW"


class SourceChannel(str, Enum):
    REST_SYNC = "rest_sync"
    SFTP_BATCH = "sftp_batch"
    DB_CDC = "db_cdc"
    HITL_RESOLUTION = "hitl_resolution"


class RuleEventType(str, Enum):
    """Policy lifecycle events fired by the server-side webhook dispatcher."""
    RULE_CREATED = "rule.created"
    RULE_UPDATED = "rule.updated"
    RULE_APPROVED = "rule.approved"
    RULE_REVOKED = "rule.revoked"
    RULE_FLAGGED_HITL = "rule.flagged_hitl"
    TRANSACTION_EVALUATED = "transaction.evaluated"
    TRANSACTION_DENIED = "transaction.denied"
    TRANSACTION_FLAGGED = "transaction.flagged"
    HITL_RESOLVED = "hitl.resolved"


# ---------------------------------------------------------------------------
# Transaction evaluation
# ---------------------------------------------------------------------------

class TransactionPayload(BaseModel):
    """Input contract for a single trade / transaction to be evaluated."""

    transaction_id: str = Field(..., description="Caller-assigned unique id.")
    entity_type: str = Field(
        ...,
        description='e.g. "Stockbroker" — must match a compiled rule\'s target entity.',
    )
    facts: dict[str, Any] = Field(
        ...,
        description="Flat key→value fact map, e.g. {'upfront_margin_pct': 18.5}.",
    )
    source_channel: SourceChannel = SourceChannel.REST_SYNC
    broker_id: str | None = None
    callback_url: str | None = Field(
        None,
        description="Webhook URL to call when a FLAGGED transaction is resolved by HITL.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyOutcome(BaseModel):
    rule_id: str
    package: str
    allow: bool | None
    violations: list[str] = Field(default_factory=list)
    circular_number: str | None = None
    clause_number: str | None = None


class EvaluationResult(BaseModel):
    transaction_id: str
    decision: Decision
    matched_policies: list[PolicyOutcome] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    hitl_case_id: str | None = None
    evaluated_at: dt.datetime
    latency_ms: float | None = None


# ---------------------------------------------------------------------------
# Batch evaluation
# ---------------------------------------------------------------------------

class BatchRequest(BaseModel):
    batch_id: str
    transactions: list[TransactionPayload]
    result_webhook_url: str | None = None
    source_channel: SourceChannel = SourceChannel.SFTP_BATCH


class BatchJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BatchResult(BaseModel):
    batch_id: str
    status: BatchJobStatus
    total: int = 0
    allowed: int = 0
    denied: int = 0
    flagged: int = 0
    results: list[EvaluationResult] = Field(default_factory=list)
    error: str | None = None
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Rules (provenance)
# ---------------------------------------------------------------------------

class CompiledRuleSummary(BaseModel):
    """Metadata for a compiled OPA policy rule — the rule provenance record."""

    id: int
    rule_id: str
    rule_version: int
    opa_package_name: str | None
    has_rego: bool
    has_jsonlogic: bool
    hitl_status: str
    circular_number: str | None = None
    clause_number: str | None = None


class RuleProvenance(BaseModel):
    """Full provenance chain for a rule: from SEBI circular → clause → compiled rule."""

    rule_id: str
    rule_version: int
    opa_package_name: str | None
    rego_policy: str | None = Field(
        None, description="Rego source code.  Only populated when include_rego=True."
    )
    hitl_status: str
    circular_number: str | None
    circular_title: str | None
    circular_issue_date: dt.date | None
    circular_source_url: str | None
    clause_number: str | None
    clause_text: str | None
    clause_sha256: str | None
    created_at: dt.datetime


# ---------------------------------------------------------------------------
# Circulars
# ---------------------------------------------------------------------------

class CircularSummary(BaseModel):
    id: int
    circular_number: str
    title: str | None
    issue_date: str | None
    department: str | None
    source_url: str | None
    is_shared: bool
    tenant_id: str
    clause_count: int = 0


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class SandboxEvaluationResult(BaseModel):
    sandbox_run_id: str
    transaction_id: str
    decision: Decision
    matched_policies: list[PolicyOutcome] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    would_trigger_hitl: bool = False
    latency_ms: float | None = None
    note: str = ""


class SandboxBatchResult(BaseModel):
    sandbox_run_id: str
    tenant_id: str
    total: int
    allowed: int
    denied: int
    flagged: int
    results: list[SandboxEvaluationResult]
    evaluated_at: str


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class AuditTrailEntry(BaseModel):
    sequence_num: int
    broker_id: str
    transaction_id: str
    evaluated_at: dt.datetime
    circular_id: str
    section_reference: str
    rule_id: str
    evaluation_result: str
    hitl_review_id: str | None
    payload_digest: str
    current_hash: str


class ChainProofSummary(BaseModel):
    verified_at: dt.datetime
    entries_checked: int
    chain_valid: bool
    break_count: int = 0
    range_start_sequence: int | None = None
    range_end_sequence: int | None = None
    window_seal_hash: str | None = None


class AuditTrailPage(BaseModel):
    report_id: str
    generated_at: dt.datetime
    generated_by: str
    total_entries: int
    entries: list[AuditTrailEntry]
    chain_proof: ChainProofSummary
    page: int
    page_size: int
    total_pages: int


# ---------------------------------------------------------------------------
# Webhook subscription (for registering broker endpoints)
# ---------------------------------------------------------------------------

class WebhookSubscriptionRequest(BaseModel):
    """Request body for POST /v1/webhooks/subscriptions."""

    url: str = Field(..., description="HTTPS endpoint that will receive webhook POST requests.")
    events: list[RuleEventType] = Field(
        ...,
        description="List of event types to subscribe to.",
        min_length=1,
    )
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebhookSubscription(BaseModel):
    subscription_id: str
    tenant_id: str
    url: str
    events: list[str]
    is_active: bool
    description: str | None
    created_at: dt.datetime
    secret_hint: str = Field(
        ...,
        description="Last 4 characters of the signing secret — confirm you are using the right secret.",
    )


# ---------------------------------------------------------------------------
# Inbound webhook envelope (broker receiver side)
# ---------------------------------------------------------------------------

class WebhookEventEnvelope(BaseModel):
    """Shape of every inbound POST to a broker's registered webhook URL."""

    event_id: str = Field(..., description="UUID — idempotency key; deduplicate on this.")
    event_type: RuleEventType
    api_version: str = "2026-08"
    emitted_at: dt.datetime
    tenant_id: str
    payload: dict[str, Any] = Field(
        ...,
        description="Event-specific data.  Shape varies by event_type.",
    )
