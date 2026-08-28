"""API and domain models for the transaction execution service.

`TransactionPayload` is the single input contract accepted from three
different producers (synchronous broker REST calls, legacy SFTP batch
files, and DB CDC change events) so the evaluator, HITL queue, and
webhook dispatcher never need to know which channel a transaction
arrived through.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    FLAGGED = "flagged"  # ambiguous / undefined evaluation -> routed to HITL


class SourceChannel(str, Enum):
    REST_SYNC = "rest_sync"          # broker's own system calling /evaluate directly
    SFTP_BATCH = "sftp_batch"        # legacy back-office nightly/intraday batch file
    DB_CDC = "db_cdc"                # Debezium/Kafka Connect (or trigger) change event
    HITL_RESOLUTION = "hitl_resolution"  # decision replayed after a human resolved it


class TransactionPayload(BaseModel):
    """Normalized shape every producer channel is adapted into before
    evaluation. `entity_type` + `facts` mirror the `input` document
    contract the Rego compiler already assumes (see
    `app.compiler.rego_compiler` module docstring)."""

    transaction_id: str
    entity_type: str = Field(..., description='e.g. "Stockbroker" — must match ExtractedComplianceRule.target_entities.')
    facts: dict[str, Any] = Field(..., description="Flat fact map keyed by app.compiler.naming.metric_field_name(...).")
    source_channel: SourceChannel = SourceChannel.REST_SYNC
    broker_id: str | None = None
    callback_url: str | None = Field(
        None, description="Webhook URL to notify with the final decision if this transaction is FLAGGED and later resolved by HITL."
    )
    received_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyOutcome(BaseModel):
    """One compiled policy's raw verdict on a transaction."""

    rule_id: str
    package: str
    allow: bool | None = Field(None, description="None means OPA returned undefined (missing/insufficient facts).")
    violations: list[str] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    transaction_id: str
    decision: Decision
    matched_policies: list[PolicyOutcome] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    hitl_case_id: str | None = Field(None, description="Set only when decision == FLAGGED.")
    evaluated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    latency_ms: float | None = None


class BatchIngestRequest(BaseModel):
    """Accepted from the legacy SFTP landing-zone poller once it has parsed
    a batch file into rows, or directly from a caller with an in-memory
    batch. Kept separate from raw file handling so the API layer never
    has to know about the SFTP filesystem layout."""

    batch_id: str
    source_channel: SourceChannel = SourceChannel.SFTP_BATCH
    transactions: list[TransactionPayload]
    result_webhook_url: str | None = Field(None, description="Where to POST the BatchJobResult summary once processing completes.")


class BatchJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BatchJobResult(BaseModel):
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


class CDCOperation(str, Enum):
    INSERT = "c"  # Debezium op codes: c=create, u=update, d=delete, r=snapshot read
    UPDATE = "u"
    DELETE = "d"
    SNAPSHOT = "r"


class CDCEvent(BaseModel):
    """Minimal envelope compatible with a Debezium/Kafka-Connect HTTP sink
    (or a Postgres/SQL-Server trigger posting directly). Only INSERT/UPDATE
    on the legacy `transactions` table are evaluated; DELETE is recorded
    but never evaluated."""

    source_table: str
    operation: CDCOperation
    after: dict[str, Any] | None = Field(None, description="Row image post-change; required for c/u, absent for d.")
    committed_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


class HITLStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class HITLCase(BaseModel):
    """An execution-time ambiguity escalated to a human reviewer. Distinct
    from `app.compiler.models.HITLFlag`, which flags a *policy* as
    unsafe to compile; this flags a *transaction* the compiled policy
    could not confidently decide."""

    case_id: str
    transaction: TransactionPayload
    reason: str
    matched_policies: list[PolicyOutcome] = Field(default_factory=list)
    status: HITLStatus = HITLStatus.PENDING
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    resolved_at: dt.datetime | None = None
    resolved_by: str | None = None
    resolution_notes: str | None = None


class HITLResolutionRequest(BaseModel):
    decision: Decision = Field(..., description="Must be ALLOW or DENY; a human cannot re-flag a case as FLAGGED.")
    resolved_by: str
    notes: str | None = None


class WebhookEvent(BaseModel):
    """Envelope posted to OMS/RMS/broker callback URLs. Signed with HMAC-SHA256
    over the raw JSON body (see `app.execution.webhook_client`)."""

    event_type: str
    transaction_id: str
    decision: Decision
    payload: dict[str, Any] = Field(default_factory=dict)
    emitted_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
