"""Pydantic I/O models and the SQLAlchemy Core table definition for the
audit ledger.

SQLAlchemy Core (not the ORM) is used deliberately: `LedgerService` needs
full control over statement shape to compute the hash chain inside the
same transaction as the insert (see `app.ledger.service`), and Core keeps
that logic explicit rather than hidden behind ORM unit-of-work semantics.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import CheckConstraint, Column, DateTime, MetaData, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON, BigInteger, Integer

# JSONB on PostgreSQL (indexable, efficient); generic JSON elsewhere (e.g.
# SQLite in tests) via SQLAlchemy's standard cross-dialect variant idiom.
_JSON_TYPE = JSON().with_variant(JSONB, "postgresql")

# SQLite only treats a column typed exactly INTEGER (not BIGINT) as its
# implicit autoincrementing rowid alias; without this variant, `RETURNING
# id` fails under SQLite in tests. Production (PostgreSQL) still gets a
# true BIGINT identity column, matching sql/ledger_schema.sql's BIGSERIAL.
_ID_TYPE = BigInteger().with_variant(Integer, "sqlite")

metadata = MetaData()

GENESIS_HASH = "0" * 64  # previous_hash of sequence_num 0


class EvaluationOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    HITL_REVIEW = "HITL_REVIEW"


compliance_audit_ledger = Table(
    "compliance_audit_ledger",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("sequence_num", BigInteger, nullable=False),
    Column("broker_id", Text, nullable=False),
    Column("transaction_id", Text, nullable=False),
    Column("evaluated_at", DateTime(timezone=True), nullable=False),
    Column("circular_id", Text, nullable=False),
    Column("clause_hash", Text, nullable=False),
    Column("section_reference", Text, nullable=False),
    Column("rule_id", Text, nullable=False),
    Column("evaluation_result", String(16), nullable=False),
    Column("hitl_review_id", Text, nullable=True),
    Column("details", _JSON_TYPE, nullable=False, server_default="{}"),
    Column("payload_digest", String(64), nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("current_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("sequence_num", name="uq_ledger_sequence"),
    CheckConstraint("evaluation_result IN ('PASS','FAIL','HITL_REVIEW')", name="ck_evaluation_result"),
)


class ComplianceEvaluationEvent(BaseModel):
    """What `LedgerService.append_entry` accepts — one record per compliance
    evaluation. Every field here is included in `payload_digest`, so
    anything an auditor might need to confirm was NOT altered after the
    fact belongs in this model, not bolted on afterward."""

    broker_id: str
    transaction_id: str
    evaluated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    circular_id: str
    clause_hash: str = Field(..., description="ExtractedComplianceRule.source_sha256 for the clause that produced this rule.")
    section_reference: str = Field(..., description='Exact clause/section, e.g. "3.2.1" or ExtractedComplianceRule.clause_number.')

    rule_id: str
    evaluation_result: EvaluationOutcome
    hitl_review_id: str | None = Field(None, description="app.execution.models.HITLCase.case_id; required iff evaluation_result is HITL_REVIEW.")

    details: dict[str, Any] = Field(default_factory=dict, description="Extra evidence: violations, input facts snapshot, OPA package, etc.")

    @model_validator(mode="after")
    def _hitl_review_id_consistency(self) -> "ComplianceEvaluationEvent":
        if self.evaluation_result == EvaluationOutcome.HITL_REVIEW and not self.hitl_review_id:
            raise ValueError("hitl_review_id is required when evaluation_result is HITL_REVIEW.")
        if self.evaluation_result != EvaluationOutcome.HITL_REVIEW and self.hitl_review_id:
            raise ValueError("hitl_review_id must be omitted unless evaluation_result is HITL_REVIEW.")
        return self


class LedgerEntry(BaseModel):
    """One persisted, hash-chained row — the append result and the shape
    returned by verification/read paths."""

    id: int
    sequence_num: int
    broker_id: str
    transaction_id: str
    evaluated_at: dt.datetime
    circular_id: str
    clause_hash: str
    section_reference: str
    rule_id: str
    evaluation_result: EvaluationOutcome
    hitl_review_id: str | None
    details: dict[str, Any]
    payload_digest: str
    previous_hash: str
    current_hash: str
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class ChainBreak(BaseModel):
    sequence_num: int
    reason: str
    expected: str
    actual: str


class ChainVerificationResult(BaseModel):
    valid: bool
    entries_checked: int
    range_start_sequence: int | None = None
    range_end_sequence: int | None = None
    breaks: list[ChainBreak] = Field(default_factory=list)
    verified_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
