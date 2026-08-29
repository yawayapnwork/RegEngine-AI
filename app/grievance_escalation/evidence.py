"""Requirement 2: assembles the evidence package -- the exact SEBI
clause hash, the transaction payload, and the SHA-256 audit ledger
proof -- into one structured record, then renders it as the
`GrievanceEvidenceDocument` list `schemas.GrievanceSubmissionRequest`
carries.

Two DIFFERENT hashes both legitimately answer "what's the clause
hash" here, and this module keeps them distinct rather than picking
one silently:
  - `ledger_clause_hash` -- `LedgerEntry.clause_hash`, which
    `app.ledger.models.ComplianceEvaluationEvent`'s own field
    docstring documents as `ExtractedComplianceRule.source_sha256`
    (the SOURCE DOCUMENT's hash at extraction time) -- this is what
    was actually hashed into `payload_digest` for this specific
    ledger entry, so it's the historically-correct value for THIS
    transaction's proof even if the clause has since been re-extracted.
  - `canonical_clause_hash` -- `Clause.sha256` (via
    `app.parsing.hashing.sha256_of_clause`, `circular_number \x1F
    clause_number \x1F` normalized text), looked up fresh from the
    relational schema via the ledger row's `clause_ref_id` FK -- the
    CURRENT canonical hash for that clause, useful for a reviewer
    cross-checking against today's clause text, but a best-effort
    lookup (`clause_ref_id` is nullable and populated best-effort per
    `compliance_audit_ledger`'s own column comment) that may be absent.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models import Clause
from app.execution.models import TransactionPayload
from app.grievance_escalation.ledger_evidence import SingleEntryLedgerProof, build_single_entry_proof, get_ledger_entry_by_transaction_id
from app.grievance_escalation.schemas import GrievanceEvidenceDocument
from app.ledger.models import LedgerEntry, compliance_audit_ledger


@dataclass(frozen=True)
class GrievanceEvidencePackage:
    transaction: TransactionPayload
    ledger_entry: LedgerEntry
    ledger_proof: SingleEntryLedgerProof
    ledger_clause_hash: str  # see this module's docstring
    canonical_clause_hash: str | None  # see this module's docstring; None if clause_ref_id was unavailable
    circular_number: str | None
    clause_number: str

    def to_evidence_documents(self) -> list[GrievanceEvidenceDocument]:
        return [
            _document("transaction_payload", self.transaction.model_dump(mode="json")),
            _document("ledger_entry", self.ledger_entry.model_dump(mode="json")),
            _document("ledger_chain_proof", {
                "previous_hash_used": self.ledger_proof.previous_hash_used,
                "stated_current_hash": self.ledger_entry.current_hash,
                "recomputed_current_hash": self.ledger_proof.recomputed_current_hash,
                "chain_linkage_verifiable": self.ledger_proof.chain_linkage_verifiable,
                "current_hash_matches": self.ledger_proof.current_hash_matches,
            }),
            _document("clause_hash", {
                "ledger_clause_hash": self.ledger_clause_hash,
                "canonical_clause_hash": self.canonical_clause_hash,
                "circular_number": self.circular_number,
                "clause_number": self.clause_number,
            }),
        ]


def _document(label: str, payload: dict) -> GrievanceEvidenceDocument:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return GrievanceEvidenceDocument(label=label, content=content, sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())


async def build_evidence_package(ledger_engine: AsyncEngine, db: AsyncSession | None, transaction: TransactionPayload) -> GrievanceEvidencePackage:
    """`db` (a session against the MAIN application schema, not the
    ledger's) is optional -- when omitted, `canonical_clause_hash`/
    `circular_number` are left None rather than looked up. This matters
    because the live evaluation hot path
    (`app.api.execution_routes.evaluate_transaction` ->
    `app.ledger.integration.log_evaluation`) does not currently carry a
    main-schema DB session at all (only a `LedgerService`) -- requiring
    one here would mean adding a new dependency to that hot path just
    for an already-documented best-effort enrichment. A caller that
    DOES have a session (e.g. a future "regenerate evidence for grievance
    X" API route) can pass one in for the fuller cross-check."""
    entry = await get_ledger_entry_by_transaction_id(ledger_engine, transaction.transaction_id)
    if entry is None:
        raise ValueError(f"No ledger entry found for transaction_id={transaction.transaction_id!r}; cannot build evidence without a recorded ledger proof.")

    proof = await build_single_entry_proof(ledger_engine, entry)

    # `clause_ref_id` is a surrogate-key FK on the raw ledger table (see
    # app.ledger.models.compliance_audit_ledger's column comment) that
    # `LedgerEntry` (the pydantic hash-chain-relevant view) deliberately
    # does NOT declare as a field -- it's not part of the hashed payload
    # and must never be treated as such. Fetched here as a raw scalar
    # from the ledger engine directly (best-effort: nullable, may be
    # None if the service-layer lookup at append time didn't resolve
    # it), then used to look up the CURRENT canonical Clause row via the
    # separate `db` session (the main application schema, not the
    # ledger's).
    async with ledger_engine.connect() as conn:
        clause_ref_id = (await conn.execute(
            select(compliance_audit_ledger.c.clause_ref_id).where(compliance_audit_ledger.c.transaction_id == transaction.transaction_id)
        )).scalar_one_or_none()

    canonical_clause_hash: str | None = None
    circular_number: str | None = None
    if clause_ref_id is not None and db is not None:
        clause = (await db.execute(select(Clause).where(Clause.id == clause_ref_id))).scalar_one_or_none()
        if clause is not None:
            canonical_clause_hash = clause.sha256
            circular_number = clause.circular.circular_number if clause.circular else None

    return GrievanceEvidencePackage(
        transaction=transaction, ledger_entry=entry, ledger_proof=proof,
        ledger_clause_hash=entry.clause_hash, canonical_clause_hash=canonical_clause_hash,
        circular_number=circular_number, clause_number=entry.section_reference,
    )
