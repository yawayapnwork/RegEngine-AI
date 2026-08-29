"""Pydantic models for the Dead-Letter Queue: what gets stored per failed
item, and the request/response shapes the admin API (app.api.dlq_routes)
exchanges with a compliance engineer's client."""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FailureCategory(str, Enum):
    """WHERE in the pipeline the item failed -- see app.resilience.exceptions'
    module docstring for how this differs from retryability."""

    PDF_PARSING = "pdf_parsing"  # unparseable SEBI circular PDF (app.parsing)
    LLM_EXTRACTION = "llm_extraction"  # CrewAI extraction/audit agent pipeline (app.agents)
    MALFORMED_AST = "malformed_ast"  # invalid JSON-Logic AST produced by the compiler (app.compiler)
    VECTOR_INGESTION = "vector_ingestion"  # Qdrant embedding/upsert failure (app.vectorstore)
    RSS_POLLING = "rss_polling"  # SEBI RSS/HTML source polling exhausted its retries (app.ingestion)
    POLICY_SELF_HEAL_EXHAUSTED = "policy_self_heal_exhausted"  # app.healing's repair loop ran out of retries (app.healing)
    OTHER = "other"


class DLQStatus(str, Enum):
    PENDING = "pending"  # awaiting a compliance engineer's decision
    REQUEUED = "requeued"  # re-dispatched back into the pipeline; not yet known to have succeeded
    RESOLVED = "resolved"  # confirmed fixed (either the requeue succeeded, or resolved out-of-band)
    DISCARDED = "discarded"  # deliberately dropped -- not a bug, not worth reprocessing (e.g. a duplicate, a spam PDF)


class DLQEntry(BaseModel):
    """One failed unit of work, durably parked for human review. `payload`
    is exactly the JSON-serializable args/kwargs the originating Celery
    task needs to run again -- a compliance engineer edits THIS field
    (e.g. correcting a malformed URL, tweaking an extraction_backend
    override) and `POST /requeue` re-dispatches the same task with the
    edited payload, unchanged otherwise."""

    entry_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    category: FailureCategory
    task_name: str = Field(..., description="Fully-qualified Celery task name to re-dispatch on requeue, e.g. 'app.agents.tasks.extract_and_audit_clause_task'.")
    original_task_id: str | None = Field(None, description="Celery task id of the run that failed, if available (None for a failure outside any task, e.g. RSS polling's own HTTP client retries).")

    payload: dict[str, Any] = Field(..., description="JSON-serializable args/kwargs to re-dispatch task_name with. Editable via PATCH before requeueing.")

    error_type: str = Field(..., description="The failing exception's class name, e.g. 'UnsupportedFileError'.")
    error_message: str
    traceback: str | None = None

    attempt_count: int = Field(1, description="How many attempts (including this one) were made before landing here.")
    first_failed_at: dt.datetime
    last_failed_at: dt.datetime

    status: DLQStatus = DLQStatus.PENDING
    requeued_at: dt.datetime | None = None
    requeued_by: str | None = Field(None, description="Principal.subject of the compliance engineer who requeued this.")
    requeued_task_id: str | None = Field(None, description="Celery task id of the re-dispatched task, once requeued.")
    resolution_notes: str | None = None
    resolved_by: str | None = None
    resolved_at: dt.datetime | None = None


class DLQEntryUpdate(BaseModel):
    """PATCH body: replace the stored payload (a compliance engineer's
    edited failing parameters) before requeueing. Partial -- omitted
    fields in `payload` are NOT merged; the caller sends the full,
    corrected payload dict, since a partial merge on an arbitrary
    task-specific dict shape has no safe default (is a missing key
    "leave as-is" or "delete"?) and this makes the semantics explicit
    instead of guessed."""

    payload: dict[str, Any]


class DLQRequeueRequest(BaseModel):
    requeued_by: str = Field(..., description="Compliance engineer's identity -- normally taken from the authenticated Principal, not client-supplied; see app.api.dlq_routes.")
    notes: str | None = None


class DLQResolveRequest(BaseModel):
    notes: str | None = None


class DLQStats(BaseModel):
    total: int
    by_category: dict[str, int]
    by_status: dict[str, int]
