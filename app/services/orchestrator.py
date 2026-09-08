"""End-to-end regulatory ingestion and compilation orchestration service.

Executes the pipeline:
  PDF -> parse_pdf -> persist_circular -> persist_clauses -> extract_and_audit
      -> compile_rule -> persist_compiled_rule -> create_hitl_review
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import extract_and_audit_clause
from app.agents.schemas import (
    AuditedComplianceRule,
    AuditFinding,
    AuditVerdict,
    ComparisonOperator,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
    QualitativeDirective,
    Severity,
    TargetEntity,
)
from app.compiler.hitl import has_blocking_flags
from app.compiler.pipeline import compile_audited_rule
from app.config import Settings, get_settings
from app.db.models import Circular, Clause, CompiledRule, HITLReview, Tenant
from app.models import ClauseChunk, ParseResult
from app.parsing.hashing import sha256_of_bytes, sha256_of_extracted_text
from app.services.pipeline import parse_pdf_bytes

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "sebi_baseline"


class ProcessE2EResult(BaseModel):
    circular_id: int
    circular_number: str
    source_url: str | None = None
    source_filename: str | None = None
    source_retrieved_at: dt.datetime | None = None
    source_document_sha256: str
    extracted_text_sha256: str
    document_hash: str  # Kept for backward compatibility; maps to source_document_sha256
    clause_count: int
    rules_compiled: int
    review_count: int
    status: str
    reviews: list[str]
    compiled_rule_ids: list[str]


class CircularStatusResult(BaseModel):
    circular_id: int
    circular_number: str
    status: str
    source_url: str | None = None
    source_filename: str | None = None
    source_retrieved_at: dt.datetime | None = None
    source_document_sha256: str | None = None
    extracted_text_sha256: str | None = None
    raw_text_digest: str  # Historical extracted text digest preserved
    clause_count: int
    compiled_rule_count: int
    hitl_review_count: int
    pending_reviews: int
    resolved_reviews: int
    active_rules: int
    reviews: list[dict[str, Any]]
    rules: list[dict[str, Any]]


class E2EOrchestrator:
    """Coordinates parsing, transactional persistence, clause extraction/auditing,
    rule compilation, and HITL review creation."""

    def __init__(
        self,
        settings: Settings | None = None,
        clause_concurrency: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.clause_concurrency = clause_concurrency or getattr(self.settings, "clause_concurrency", 3)

    async def ensure_baseline_tenant(
        self, session: AsyncSession, tenant_id: str = DEFAULT_TENANT_ID
    ) -> Tenant:
        """Ensures the baseline tenant row exists before inserting foreign keys."""
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            tenant = Tenant(
                tenant_id=tenant_id,
                display_name="SEBI Baseline / Shared Regulatory Corpus",
                tenant_type="stockbroker",
                sebi_reg_number=None,
                is_active=True,
                opa_bundle_prefix="baseline",
                risk_overlay={},
            )
            session.add(tenant)
            await session.flush()
            logger.info("Created baseline tenant '%s'.", tenant_id)
        return tenant

    async def parse_pdf(
        self, file_bytes: bytes, filename: str | None = None
    ) -> ParseResult:
        return await parse_pdf_bytes(file_bytes, filename=filename, settings=self.settings)

    async def persist_circular(
        self,
        session: AsyncSession,
        parsed: ParseResult,
        file_bytes: bytes,
        filename: str | None,
        tenant_id: str = DEFAULT_TENANT_ID,
        source_url: str | None = None,
        source_retrieved_at: dt.datetime | None = None,
    ) -> Circular:
        """Persists the Circular entity with source document SHA-256 digest,
        extracted text digest, and clean source metadata."""
        await self.ensure_baseline_tenant(session, tenant_id)

        # Requirement 1 & 8: Calculate SHA-256 over raw uploaded PDF bytes
        source_document_sha256 = (
            parsed.source_document_sha256
            if parsed and parsed.source_document_sha256
            else sha256_of_bytes(file_bytes)
        )

        # Requirement 3: Separately calculate SHA-256 of normalized/extracted text
        combined_text = "\n\n".join(c.text for c in parsed.chunks) if parsed.chunks else file_bytes.decode("utf-8", errors="ignore")
        raw_text_digest = (
            parsed.extracted_text_sha256
            if parsed and parsed.extracted_text_sha256
            else sha256_of_extracted_text(combined_text)
        )

        circular_number = parsed.metadata.circular_number
        if not circular_number:
            circular_number = f"SEBI/CIR/{raw_text_digest[:10].upper()}"

        # Clean source_url and source_filename:
        # Never put a filename into source_url. source_url must be a valid external URL starting with http://, https://, or ftp://.
        candidate_url = source_url or (parsed.metadata.source_url if parsed and parsed.metadata else None)
        valid_source_url = None
        if candidate_url and (candidate_url.startswith("http://") or candidate_url.startswith("https://") or candidate_url.startswith("ftp://")):
            valid_source_url = candidate_url

        # Clean source_filename:
        # Preserve actual filename separately.
        candidate_filename = (
            filename
            or (parsed.metadata.source_filename if parsed and parsed.metadata else None)
            or None
        )
        clean_filename = None
        if candidate_filename:
            from pathlib import Path
            clean_filename = Path(candidate_filename).name

        retrieved_at = (
            source_retrieved_at
            or (parsed.metadata.source_retrieved_at if parsed and parsed.metadata else None)
        )

        existing = (
            await session.execute(
                select(Circular).where(
                    (Circular.circular_number == circular_number)
                    | (Circular.raw_text_digest == raw_text_digest)
                    | (Circular.source_document_sha256 == source_document_sha256)
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            updated = False
            if existing.source_document_sha256 is None and source_document_sha256:
                existing.source_document_sha256 = source_document_sha256
                updated = True
            if existing.source_filename is None and clean_filename:
                existing.source_filename = clean_filename
                updated = True
            if existing.source_url is None and valid_source_url:
                existing.source_url = valid_source_url
                updated = True
            if existing.source_retrieved_at is None and retrieved_at:
                existing.source_retrieved_at = retrieved_at
                updated = True
            if updated:
                await session.flush()
            return existing

        circular = Circular(
            tenant_id=tenant_id,
            is_shared=True,
            circular_number=circular_number,
            title=parsed.metadata.title or clean_filename or "SEBI Circular",
            issue_date=parsed.metadata.issue_date or dt.date.today(),
            source_url=valid_source_url,
            source_filename=clean_filename,
            source_retrieved_at=retrieved_at,
            department=parsed.metadata.department or "MRD",
            source_document_sha256=source_document_sha256,
            raw_text_digest=raw_text_digest,
        )
        session.add(circular)
        await session.flush()
        return circular

    async def persist_clauses(
        self,
        session: AsyncSession,
        circular: Circular,
        chunks: list[ClauseChunk],
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[Clause]:
        """Persists layout-aware Clause entities linked to the circular."""
        clauses: list[Clause] = []
        for chunk in chunks:
            sha256 = chunk.sha256 if (chunk.sha256 and len(chunk.sha256) == 64) else hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()

            # Check if clause already exists in this circular
            existing = (
                await session.execute(
                    select(Clause).where(
                        Clause.circular_id == circular.id,
                        Clause.sha256 == sha256,
                    )
                )
            ).scalar_one_or_none()

            if existing is not None:
                clauses.append(existing)
                continue

            clause = Clause(
                circular_id=circular.id,
                tenant_id=tenant_id,
                clause_number=chunk.clause_number,
                section_title=chunk.section_title,
                section_path=chunk.section_path or [],
                element_kind="clause",
                text=chunk.text,
                sha256=sha256,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                contains_table=chunk.contains_table,
            )
            session.add(clause)
            clauses.append(clause)

        await session.flush()
        return clauses

    async def extract_and_audit(
        self,
        chunk: ClauseChunk,
        sibling_chunks: list[dict] | None = None,
    ) -> AuditedComplianceRule:
        """Extracts and audits compliance rules from a clause chunk through the
        configured LLM provider abstraction (offline, openai, anthropic, huggingface)."""
        return await extract_and_audit_clause(chunk, sibling_chunks, self.settings)

    async def extract_and_audit_circular(
        self,
        chunks: list[ClauseChunk],
        sibling_chunks: list[dict] | None = None,
        max_concurrency: int | None = None,
    ) -> list[tuple[ClauseChunk, AuditedComplianceRule | None, Exception | None]]:
        """Extracts and audits compliance rules for all chunks of a circular using bounded
        concurrency, preserving chunk ordering while capturing per-clause exceptions.

        Uses an asyncio.Semaphore to guarantee that in-flight LLM calls never exceed
        the configured concurrency limit (clause_concurrency).
        """
        if not chunks:
            return []

        concurrency = max_concurrency or self.clause_concurrency
        gate = asyncio.Semaphore(max(1, concurrency))

        siblings = sibling_chunks or [
            {"chunk_id": c.chunk_id, "clause_number": c.clause_number, "section_path": c.section_path, "text": c.text}
            for c in chunks
        ]

        async def _run_one(idx: int, chunk: ClauseChunk) -> tuple[int, ClauseChunk, AuditedComplianceRule | None, Exception | None]:
            async with gate:
                try:
                    audited = await self.extract_and_audit(chunk, siblings)
                    return idx, chunk, audited, None
                except Exception as exc:
                    logger.error(
                        "Clause extraction/audit failed for chunk %s (clause %s): %s",
                        chunk.chunk_id,
                        chunk.clause_number,
                        exc,
                        exc_info=True,
                    )
                    return idx, chunk, None, exc

        tasks = [_run_one(i, c) for i, c in enumerate(chunks)]
        results = await asyncio.gather(*tasks)
        # Guarantee strictly deterministic ordering matching the input chunk sequence
        results_list = list(results)
        results_list.sort(key=lambda r: r[0])
        return [(r[1], r[2], r[3]) for r in results_list]

    async def _persist_failed_clause_rule(
        self,
        session: AsyncSession,
        clause: Clause,
        chunk: ClauseChunk,
        error: Exception | None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> tuple[CompiledRule, list[HITLReview]]:
        """Explicitly handles and persists a failed clause extraction/audit as an uncompiled,
        non-deployable rule with a blocking HITL review."""
        rule_id = f"RULE-FAILED-{clause.clause_number or chunk.chunk_id or str(clause.id)}"
        existing_rule = (
            await session.execute(
                select(CompiledRule).where(
                    CompiledRule.rule_id == rule_id,
                    CompiledRule.clause_id == clause.id,
                )
            )
        ).scalar_one_or_none()

        if existing_rule is not None:
            compiled_rule = existing_rule
        else:
            compiled_rule = CompiledRule(
                clause_id=clause.id,
                tenant_id=tenant_id,
                rule_id=rule_id,
                rule_version=1,
                rego_policy=None,
                opa_package_name=None,
                jsonlogic_ast=None,
                is_compiled=False,
                is_active=False,  # Failed extraction can NEVER be active!
                hitl_status="BLOCKING",
                compiler_version="1.0.0",
            )
            session.add(compiled_rule)
            await session.flush()

        error_msg = f"{type(error).__name__}: {str(error)}" if error else "Extraction failed"
        rev = HITLReview(
            review_id=str(uuid.uuid4()),
            clause_id=clause.id,
            compiled_rule_id=compiled_rule.id,
            tenant_id=tenant_id,
            reason_code="audit_not_approved",
            severity="blocking",
            description=f"Automated clause extraction failed: {error_msg}. Manual compliance review required.",
            source_excerpt=clause.text[:200] if clause.text else None,
            field_path="deterministic_logic",
            status="PENDING",
            resolved_at=None,
        )
        session.add(rev)
        await session.flush()
        return compiled_rule, [rev]

    async def compile_and_persist_rules(
        self,
        session: AsyncSession,
        clause: Clause,
        audited: AuditedComplianceRule,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> tuple[CompiledRule, list[HITLReview]]:
        """Compiles an audited rule and persists CompiledRule and HITLReview records."""
        compilation = compile_audited_rule(audited)

        # Check existing rule
        rule_id = audited.rule.rule_id
        existing_rule = (
            await session.execute(
                select(CompiledRule).where(
                    CompiledRule.rule_id == rule_id,
                    CompiledRule.clause_id == clause.id,
                )
            )
        ).scalar_one_or_none()

        hitl_status = (
            "BLOCKING"
            if has_blocking_flags(compilation.hitl_flags)
            else ("ADVISORY" if compilation.hitl_flags else "NONE")
        )

        if existing_rule is not None:
            compiled_rule = existing_rule
        else:
            compiled_rule = CompiledRule(
                clause_id=clause.id,
                tenant_id=tenant_id,
                rule_id=rule_id,
                rule_version=1,
                rego_policy=compilation.rego.rego_code if compilation.rego else None,
                opa_package_name=compilation.rego.package if compilation.rego else None,
                jsonlogic_ast=compilation.json_logic.logic if compilation.json_logic else None,
                is_compiled=compilation.compiled,
                is_active=False,  # inactive until approved!
                hitl_status=hitl_status,
                compiler_version="1.0.0",
            )
            session.add(compiled_rule)
            await session.flush()

        # Create HITLReview records referencing both Clause and CompiledRule
        reviews: list[HITLReview] = []

        # Find existing reviews for this rule
        existing_reviews = (
            await session.execute(
                select(HITLReview).where(HITLReview.compiled_rule_id == compiled_rule.id)
            )
        ).scalars().all()
        if existing_reviews:
            reviews.extend(existing_reviews)
        else:
            if compilation.hitl_flags:
                for flag in compilation.hitl_flags:
                    rev = HITLReview(
                        review_id=flag.flag_id or str(uuid.uuid4()),
                        clause_id=clause.id,
                        compiled_rule_id=compiled_rule.id,
                        tenant_id=tenant_id,
                        reason_code=flag.reason_code.value,
                        severity=flag.severity.value,
                        description=flag.description,
                        source_excerpt=flag.source_excerpt,
                        field_path=flag.field_path,
                        status="PENDING",
                        resolved_at=None,
                    )
                    session.add(rev)
                    reviews.append(rev)
            else:
                # Every newly compiled regulatory rule requires human approval before live execution
                rev = HITLReview(
                    review_id=str(uuid.uuid4()),
                    clause_id=clause.id,
                    compiled_rule_id=compiled_rule.id,
                    tenant_id=tenant_id,
                    reason_code="low_extraction_confidence",
                    severity="blocking",
                    description=f"Initial compliance sign-off required for compiled rule {compiled_rule.rule_id}",
                    source_excerpt=clause.text[:200] if clause.text else None,
                    field_path="deterministic_logic",
                    status="PENDING",
                    resolved_at=None,
                )
                session.add(rev)
                reviews.append(rev)

            await session.flush()

        return compiled_rule, reviews

    async def process_circular_pdf(
        self,
        session: AsyncSession,
        file_bytes: bytes,
        filename: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        source_url: str | None = None,
        source_retrieved_at: dt.datetime | None = None,
    ) -> ProcessE2EResult:
        """Executes the complete transactional pipeline."""
        # 1. Parse PDF
        parsed = await self.parse_pdf(file_bytes, filename=filename)
        if source_url and (source_url.startswith("http://") or source_url.startswith("https://") or source_url.startswith("ftp://")):
            parsed.metadata.source_url = source_url
        if source_retrieved_at:
            parsed.metadata.source_retrieved_at = source_retrieved_at

        # 2. Persist Circular
        circular = await self.persist_circular(
            session=session,
            parsed=parsed,
            file_bytes=file_bytes,
            filename=filename,
            tenant_id=tenant_id,
            source_url=source_url,
            source_retrieved_at=source_retrieved_at,
        )

        # 3. Persist Clauses
        clauses = await self.persist_clauses(session, circular, parsed.chunks, tenant_id)

        # 4. Extract, Audit, Compile, and Create HITL Reviews with bounded concurrency
        sibling_payload = [
            {"chunk_id": c.chunk_id, "clause_number": c.clause_number, "section_path": c.section_path, "text": c.text}
            for c in parsed.chunks
        ]

        # Concurrently extract and audit chunks with bounded concurrency
        extraction_results = await self.extract_and_audit_circular(
            parsed.chunks, sibling_chunks=sibling_payload
        )

        compiled_rule_ids: list[str] = []
        review_ids: list[str] = []
        rules_compiled_count = 0

        # Persist rules and reviews sequentially in strictly deterministic chunk order
        clause_map = {c.sha256: c for c in clauses}
        for i, (chunk, audited, error) in enumerate(extraction_results):
            clause = clauses[i] if i < len(clauses) else clause_map.get(chunk.sha256)
            if not clause:
                logger.error("No persisted clause found for chunk %s (%s); skipping", chunk.chunk_id, chunk.clause_number)
                continue

            if error is not None or audited is None:
                # Individual clause failure handled explicitly
                rule, reviews = await self._persist_failed_clause_rule(
                    session=session,
                    clause=clause,
                    chunk=chunk,
                    error=error,
                    tenant_id=tenant_id,
                )
            else:
                rule, reviews = await self.compile_and_persist_rules(
                    session=session,
                    clause=clause,
                    audited=audited,
                    tenant_id=tenant_id,
                )

            if rule.is_compiled:
                rules_compiled_count += 1
            compiled_rule_ids.append(rule.rule_id)
            for r in reviews:
                review_ids.append(r.review_id)

        await session.commit()

        return ProcessE2EResult(
            circular_id=circular.id,
            circular_number=circular.circular_number,
            source_url=circular.source_url,
            source_filename=circular.source_filename,
            source_retrieved_at=circular.source_retrieved_at,
            source_document_sha256=circular.source_document_sha256 or "",
            extracted_text_sha256=circular.raw_text_digest,
            document_hash=circular.source_document_sha256 or circular.raw_text_digest,
            clause_count=len(clauses),
            rules_compiled=rules_compiled_count,
            review_count=len(review_ids),
            status="review_required" if review_ids else "approved",
            reviews=review_ids,
            compiled_rule_ids=compiled_rule_ids,
        )

    async def get_circular_status(
        self, session: AsyncSession, circular_id: int
    ) -> CircularStatusResult | None:
        """Determines the current lifecycle state of a processed circular."""
        circular = await session.get(Circular, circular_id)
        if circular is None:
            return None

        clauses = (
            await session.execute(
                select(Clause).where(Clause.circular_id == circular_id)
            )
        ).scalars().all()

        clause_ids = [c.id for c in clauses]
        rules: list[CompiledRule] = []
        reviews: list[HITLReview] = []

        if clause_ids:
            rules = list(
                (
                    await session.execute(
                        select(CompiledRule).where(CompiledRule.clause_id.in_(clause_ids))
                    )
                ).scalars().all()
            )
            reviews = list(
                (
                    await session.execute(
                        select(HITLReview).where(HITLReview.clause_id.in_(clause_ids))
                    )
                ).scalars().all()
            )

        pending_reviews = sum(1 for r in reviews if r.status in ("PENDING", "IN_REVIEW"))
        resolved_reviews = sum(1 for r in reviews if r.status == "RESOLVED")
        active_rules = sum(1 for r in rules if r.is_active)

        if not clauses or not rules:
            status = "processing"
        elif active_rules > 0:
            status = "deployed"
        elif pending_reviews > 0:
            status = "review_required"
        elif any(r.status == "REJECTED" for r in reviews):
            status = "failed"
        elif resolved_reviews > 0:
            status = "approved"
        else:
            status = "processing"

        return CircularStatusResult(
            circular_id=circular.id,
            circular_number=circular.circular_number,
            status=status,
            source_url=circular.source_url,
            source_filename=circular.source_filename,
            source_retrieved_at=circular.source_retrieved_at,
            source_document_sha256=circular.source_document_sha256,
            extracted_text_sha256=circular.raw_text_digest,
            raw_text_digest=circular.raw_text_digest,
            clause_count=len(clauses),
            compiled_rule_count=len(rules),
            hitl_review_count=len(reviews),
            pending_reviews=pending_reviews,
            resolved_reviews=resolved_reviews,
            active_rules=active_rules,
            reviews=[{"review_id": r.review_id, "status": r.status, "severity": r.severity} for r in reviews],
            rules=[{"rule_id": r.rule_id, "is_active": r.is_active, "hitl_status": r.hitl_status} for r in rules],
        )

