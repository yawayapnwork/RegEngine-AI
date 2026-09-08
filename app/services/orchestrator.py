"""End-to-end regulatory ingestion and compilation orchestration service.

Executes the pipeline:
  PDF -> parse_pdf -> persist_circular -> persist_clauses -> extract_and_audit
      -> compile_rule -> persist_compiled_rule -> create_hitl_review
"""
from __future__ import annotations

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
from app.services.pipeline import parse_pdf_bytes

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = "sebi_baseline"


class ProcessE2EResult(BaseModel):
    circular_id: int
    circular_number: str
    document_hash: str
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
    raw_text_digest: str
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

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

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
    ) -> Circular:
        """Persists the Circular entity with document SHA-256 digest and metadata."""
        await self.ensure_baseline_tenant(session, tenant_id)

        combined_text = "\n\n".join(c.text for c in parsed.chunks) if parsed.chunks else file_bytes.decode("utf-8", errors="ignore")
        raw_text_digest = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()

        circular_number = parsed.metadata.circular_number
        if not circular_number:
            circular_number = f"SEBI/CIR/{raw_text_digest[:10].upper()}"

        existing = (
            await session.execute(
                select(Circular).where(
                    (Circular.circular_number == circular_number)
                    | (Circular.raw_text_digest == raw_text_digest)
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            return existing

        circular = Circular(
            tenant_id=tenant_id,
            is_shared=True,
            circular_number=circular_number,
            title=parsed.metadata.title or filename or "SEBI Circular",
            issue_date=parsed.metadata.issue_date or dt.date.today(),
            source_url=filename or None,
            department=parsed.metadata.department or "MRD",
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
    ) -> ProcessE2EResult:
        """Executes the complete transactional pipeline."""
        # 1. Parse PDF
        parsed = await self.parse_pdf(file_bytes, filename=filename)

        # 2. Persist Circular
        circular = await self.persist_circular(session, parsed, file_bytes, filename, tenant_id)

        # 3. Persist Clauses
        clauses = await self.persist_clauses(session, circular, parsed.chunks, tenant_id)

        # 4. Extract, Audit, Compile, and Create HITL Reviews
        sibling_payload = [
            {"chunk_id": c.chunk_id, "clause_number": c.clause_number, "section_path": c.section_path, "text": c.text}
            for c in parsed.chunks
        ]

        compiled_rule_ids: list[str] = []
        review_ids: list[str] = []
        rules_compiled_count = 0

        # Prioritize clauses with obligations/percentages, and ensure all clauses are processed
        clause_map = {c.sha256: c for c in clauses}
        for chunk in parsed.chunks:
            clause = clause_map.get(chunk.sha256)
            if not clause:
                continue

            audited = await self.extract_and_audit(chunk, sibling_payload)
            rule, reviews = await self.compile_and_persist_rules(session, clause, audited, tenant_id)

            if rule.is_compiled:
                rules_compiled_count += 1
            compiled_rule_ids.append(rule.rule_id)
            for r in reviews:
                review_ids.append(r.review_id)

        await session.commit()

        return ProcessE2EResult(
            circular_id=circular.id,
            circular_number=circular.circular_number,
            document_hash=circular.raw_text_digest,
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
