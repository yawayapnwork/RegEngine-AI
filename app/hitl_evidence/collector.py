"""Collector and synthesizer for the Unified HITL Review Evidence package.

Strictly enforces:
1. Multi-Tenant Isolation: Every database query, vector search, and cache lookup
   is scoped strictly to the review's tenant_id.
2. Invariant: Roadmap capabilities are read-only and NEVER mutate rules or auto-approve.
3. Feature Toggles: Subsystems that are disabled return `EvidenceStatus.DISABLED`
   gracefully without failing the overall review screen.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Clause, CompiledRule, HITLReview
from app.hitl_evidence.models import (
    ArbitrationEvidenceSection,
    DeterministicEvidenceSection,
    DigitalTwinEvidenceSection,
    EvidenceStatus,
    MNAEvidenceSection,
    MNAFindingItem,
    PrecedentEvidenceSection,
    PrecedentItem,
    SourceEvidenceSection,
    UnifiedReviewEvidence,
    ZKPEvidenceSection,
    ZKPProofItem,
)
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)


class HITLEvidenceCollector:
    """Assembles the unified evidence dossier for a specific HITL review case."""

    @staticmethod
    async def collect_review_evidence(
        session: AsyncSession,
        review_id: str,
        principal: Principal,
        settings: Settings,
    ) -> UnifiedReviewEvidence:
        # 1. Fetch HITLReview
        review_query = select(HITLReview).where(HITLReview.review_id == review_id)
        result = await session.execute(review_query)
        review = result.scalar_one_or_none()
        if review is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No HITL review found with ID '{review_id}'.",
            )

        # 2. Authorization & Tenant Isolation check:
        # System_Admin can inspect any tenant's review; Compliance_Officer is strictly scoped to their tenant.
        is_system_admin = Role.SYSTEM_ADMIN in principal.roles
        if not is_system_admin and principal.tenant_id != review.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Principal '{principal.subject}' does not have access to tenant '{review.tenant_id}'.",
            )


        # 3. Source Evidence (Clause & Circular)
        clause_query = select(Clause).where(
            Clause.id == review.clause_id,
            Clause.tenant_id == review.tenant_id,
        )
        clause_result = await session.execute(clause_query)
        clause = clause_result.scalar_one_or_none()

        circular_number = None
        circular_id = None
        if clause:
            circular_id = clause.circular_id
            if clause.circular:
                circular_number = clause.circular.circular_number
            elif clause.circular_id:
                from app.db.models import Circular
                circ_res = await session.execute(select(Circular.circular_number).where(Circular.id == clause.circular_id))
                circular_number = circ_res.scalar_one_or_none()

        source_section = SourceEvidenceSection(
            status=EvidenceStatus.AVAILABLE if clause else EvidenceStatus.NOT_FOUND,
            circular_id=circular_id,
            circular_number=circular_number,
            clause_id=clause.id if clause else review.clause_id,
            clause_number=clause.clause_number if clause else None,
            section_title=clause.section_title if clause else None,
            section_path=clause.section_path if clause else [],
            raw_text=(clause.text if clause else None) or review.source_excerpt or review.description,
            source_sha256=clause.sha256 if clause else "",
            page_start=clause.page_start if clause else None,
            page_end=clause.page_end if clause else None,
        )

        # 4. Deterministic Evidence (CompiledRule & AST)
        compiled_rule: CompiledRule | None = None
        if review.compiled_rule_id:
            rule_query = select(CompiledRule).where(
                CompiledRule.id == review.compiled_rule_id,
                CompiledRule.tenant_id == review.tenant_id,
            )
            rule_result = await session.execute(rule_query)
            compiled_rule = rule_result.scalar_one_or_none()

        rule_id = compiled_rule.rule_id if compiled_rule else (f"rule-clause-{review.clause_id}")
        rule_version = compiled_rule.rule_version if compiled_rule else 1
        policy_sha256 = compiled_rule.policy_sha256 if compiled_rule else ""

        # Extract canonical facts referenced in AST
        canonical_facts: dict[str, Any] = {}
        if compiled_rule and compiled_rule.jsonlogic_ast:
            # Recursively pull {"var": ...} keys
            def extract_vars(ast: Any) -> list[str]:
                found = []
                if isinstance(ast, dict):
                    if "var" in ast:
                        found.append(str(ast["var"]))
                    for v in ast.values():
                        found.extend(extract_vars(v))
                elif isinstance(ast, list):
                    for item in ast:
                        found.extend(extract_vars(item))
                return found

            vars_found = set(extract_vars(compiled_rule.jsonlogic_ast))
            canonical_facts = {var_name: "required_by_rule_ast" for var_name in sorted(vars_found)}

        deterministic_section = DeterministicEvidenceSection(
            status=EvidenceStatus.AVAILABLE if compiled_rule else EvidenceStatus.NOT_FOUND,
            rule_id=rule_id,
            rule_version=rule_version,
            policy_sha256=policy_sha256,
            canonical_facts=canonical_facts,
            jsonlogic_ast=compiled_rule.jsonlogic_ast if compiled_rule else None,
            rego_policy=compiled_rule.rego_policy if compiled_rule else None,
            opa_package_name=compiled_rule.opa_package_name if compiled_rule else None,
            is_active=compiled_rule.is_active if compiled_rule else False,
            hitl_status=compiled_rule.hitl_status if compiled_rule else review.status,
        )

        # 5. Historical Precedent Evidence (Case-Law Memory)
        precedent_section = await HITLEvidenceCollector._collect_precedents(
            clause_text=source_section.raw_text,
            clause_number=source_section.clause_number,
            tenant_id=review.tenant_id,
            settings=settings,
        )

        # 6. AI-Generated Analysis (Arbitration Dialogue)
        arbitration_section = await HITLEvidenceCollector._collect_arbitration(
            tenant_id=review.tenant_id,
            circular_id=str(circular_number or circular_id or "default"),
            clause_id=str(source_section.clause_number or review.clause_id),
            settings=settings,
        )

        # 7. Simulation (Digital Twin Rule-Impact Preview)
        digital_twin_section = HITLEvidenceCollector._collect_digital_twin_preview(
            review_id=review.review_id,
            tenant_id=review.tenant_id,
            settings=settings,
        )

        # 8. Cryptographic Proof (ZKP Verification Evidence)
        zkp_section = await HITLEvidenceCollector._collect_zkp_evidence(
            session=session,
            tenant_id=review.tenant_id,
            rule_id=rule_id,
            clause_sha256=source_section.source_sha256,
            settings=settings,
        )

        # 9. M&A Due-Diligence Findings
        mna_section = await HITLEvidenceCollector._collect_mna_evidence(
            session=session,
            tenant_id=review.tenant_id,
            rule_id=rule_id,
            settings=settings,
        )

        # 10. Assemble Unified Evidence Envelope
        return UnifiedReviewEvidence(
            review_id=review.review_id,
            tenant_id=review.tenant_id,
            candidate_rule_id=rule_id,
            candidate_rule_version=rule_version,
            candidate_policy_sha256=policy_sha256,
            source_clause_sha256=source_section.source_sha256,
            approval_status=review.status,
            source_evidence=source_section,
            deterministic_evidence=deterministic_section,
            arbitration_analysis=arbitration_section,
            precedent_evidence=precedent_section,
            digital_twin_simulation=digital_twin_section,
            zkp_evidence=zkp_section,
            mna_findings=mna_section,
        )

    @staticmethod
    async def _collect_precedents(
        clause_text: str,
        clause_number: str | None,
        tenant_id: str,
        settings: Settings,
    ) -> PrecedentEvidenceSection:
        if not getattr(settings, "case_law_memory_enabled", False):
            return PrecedentEvidenceSection(
                status=EvidenceStatus.DISABLED,
                notes="Case-Law Memory feature is disabled by configuration (case_law_memory_enabled=False).",
            )

        try:
            from app.case_law import get_case_law_agent

            agent = get_case_law_agent()
            analysis = await agent.analyze_clause(
                clause_text=clause_text,
                clause_number=clause_number,
                tenant_id=tenant_id,
                top_k=settings.case_law_top_k,
                min_similarity=settings.case_law_similarity_threshold,
            )

            items = [
                PrecedentItem(
                    precedent_id=p.precedent_id,
                    circular_id=p.circular_id,
                    clause_id=p.clause_id,
                    clause_number=p.clause_number,
                    similarity_score=p.similarity_score,
                    resolution_status=p.resolution_status,
                    decision_rationales=p.decision_rationales,
                    approved_notes=p.approved_notes,
                    source_clause_text=p.source_clause_text,
                )
                for p in analysis.precedents
            ]

            return PrecedentEvidenceSection(
                status=EvidenceStatus.AVAILABLE if items else EvidenceStatus.NOT_FOUND,
                precedents_count=len(items),
                precedents=items,
                reasoning_summary=analysis.recommendation,
            )
        except Exception as exc:
            logger.warning("Failed to collect case-law precedents: %s", exc)
            return PrecedentEvidenceSection(
                status=EvidenceStatus.ERROR,
                notes=f"Precedent search encountered an error: {str(exc)}",
            )

    @staticmethod
    async def _collect_arbitration(
        tenant_id: str,
        circular_id: str,
        clause_id: str,
        settings: Settings,
    ) -> ArbitrationEvidenceSection:
        if not getattr(settings, "arbitration_enabled", False):
            return ArbitrationEvidenceSection(
                status=EvidenceStatus.DISABLED,
                notes="Multi-Agent Arbitration is disabled by configuration (arbitration_enabled=False).",
            )

        try:
            from app.negotiation.arbitration_store import ArbitrationStore
            from app.negotiation.hitl_bridge import format_arbitration_transcript_for_hitl

            store = ArbitrationStore(settings)
            # Retrieve transcript for session
            session_id = f"arb-{tenant_id}-{clause_id}"
            transcript = await store.get_transcript(tenant_id=tenant_id, session_id=session_id)
            if not transcript:
                return ArbitrationEvidenceSection(
                    status=EvidenceStatus.NOT_FOUND,
                    notes="No arbitration transcript found for this clause and tenant.",
                )

            formatted = format_arbitration_transcript_for_hitl(transcript)
            verdict = transcript.arbiter_verdict

            return ArbitrationEvidenceSection(
                status=EvidenceStatus.AVAILABLE,
                session_id=transcript.session_id,
                final_outcome=transcript.final_outcome.value if transcript.final_outcome else None,
                arbiter_confidence=verdict.confidence if verdict else None,
                same_model_risk=verdict.same_model_risk if verdict else False,
                security_flags=verdict.security_flags if verdict else [],
                transcript_sha256=transcript.transcript_sha256,
                formatted_transcript=formatted,
            )
        except Exception as exc:
            logger.warning("Failed to collect arbitration transcript: %s", exc)
            return ArbitrationEvidenceSection(
                status=EvidenceStatus.ERROR,
                notes=f"Arbitration transcript retrieval error: {str(exc)}",
            )

    @staticmethod
    def _collect_digital_twin_preview(
        review_id: str,
        tenant_id: str,
        settings: Settings,
    ) -> DigitalTwinEvidenceSection:
        from app.backtest.tasks import get_preview_report

        report = get_preview_report(f"preview-{review_id}")
        if not report:
            is_enabled = getattr(settings, "rule_preview_enabled", False)
            if not is_enabled:
                return DigitalTwinEvidenceSection(
                    status=EvidenceStatus.DISABLED,
                    notes="Digital Twin Rule-Impact Preview is disabled by configuration (rule_preview_enabled=False).",
                )
            return DigitalTwinEvidenceSection(
                status=EvidenceStatus.NOT_FOUND,
                notes="No historical replay preview report has been generated for this review case yet.",
            )

        # Enforce tenant isolation invariant on cached report
        if report.scope and report.scope.tenant_id != tenant_id:
            logger.error(
                "CRITICAL: Digital twin report tenant mismatch! Expected %s, got %s",
                tenant_id,
                report.scope.tenant_id,
            )
            return DigitalTwinEvidenceSection(
                status=EvidenceStatus.ERROR,
                notes="Security isolation violation: preview report does not belong to this tenant.",
            )

        total_eval = getattr(report, "total_evaluated", getattr(report, "total_transactions_evaluated", 0))
        old_fails = getattr(report, "old_fail_count", getattr(report, "baseline_fail_count", 0))
        new_fails = getattr(report, "new_fail_count", getattr(report, "candidate_fail_count", 0))
        newly_failing = getattr(report, "newly_affected", getattr(report, "newly_failing_count", 0))
        newly_passing = getattr(report, "no_longer_affected", getattr(report, "newly_passing_count", 0))
        delta_rate = getattr(report, "delta_failure_rate_pct", getattr(report, "failure_rate_delta", float(new_fails - old_fails)))

        financial_impact = getattr(report, "financial_impact", getattr(report, "aggregate_financial_impact", None))
        financial_dict = financial_impact.model_dump() if financial_impact and hasattr(financial_impact, "model_dump") else (financial_impact if isinstance(financial_impact, dict) else None)

        return DigitalTwinEvidenceSection(
            status=EvidenceStatus.AVAILABLE,
            report_id=getattr(report, "preview_id", None),
            lookback_days=report.scope.lookback_days if report.scope else None,
            transactions_evaluated=total_eval,
            baseline_pass_count=max(0, total_eval - old_fails),
            baseline_fail_count=old_fails,
            candidate_pass_count=max(0, total_eval - new_fails),
            candidate_fail_count=new_fails,
            newly_failing_count=newly_failing,
            newly_passing_count=newly_passing,
            failure_rate_delta=delta_rate,
            aggregate_financial_impact=financial_dict,
            dataset_snapshot_hash=report.dataset_snapshot_hash,
            result_digest=report.result_digest,
        )

    @staticmethod
    async def _collect_zkp_evidence(
        session: AsyncSession,
        tenant_id: str,
        rule_id: str,
        clause_sha256: str,
        settings: Settings,
    ) -> ZKPEvidenceSection:
        if not getattr(settings, "zkp_enabled", False):
            return ZKPEvidenceSection(
                status=EvidenceStatus.DISABLED,
                notes="Zero-Knowledge Proof verification is disabled by configuration (zkp_enabled=False).",
            )

        try:
            from app.ledger.models import compliance_audit_ledger

            # Query ledger for zk_proof entries matching this tenant and rule
            query = (
                select(compliance_audit_ledger)
                .where(
                    compliance_audit_ledger.c.broker_id == tenant_id,
                    compliance_audit_ledger.c.rule_id == rule_id,
                )
                .order_by(compliance_audit_ledger.c.sequence_num.desc())
                .limit(10)
            )

            res = await session.execute(query)
            ledger_rows = res.mappings().all()

            proof_items: list[ZKPProofItem] = []
            for row in ledger_rows:
                details = row.get("details") or {}
                zk_data = details.get("zk_proof") or details.get("zk_collateral_proof")
                if zk_data:
                    proof_items.append(
                        ZKPProofItem(
                            circuit_id=zk_data.get("circuit_id", "margin_compliance_v1"),
                            proof_hash=zk_data.get("proof_hash", ""),
                            verified=True,
                            ledger_sequence_num=row.get("sequence_num"),
                            timestamp=row.get("evaluated_at").isoformat() if row.get("evaluated_at") else None,
                            public_signals=zk_data.get("public_signals", []),
                        )
                    )

            return ZKPEvidenceSection(
                status=EvidenceStatus.AVAILABLE if proof_items else EvidenceStatus.NOT_FOUND,
                proof_count=len(proof_items),
                proofs=proof_items,
            )
        except Exception as exc:
            logger.warning("Failed to query ZKP evidence from ledger: %s", exc)
            return ZKPEvidenceSection(
                status=EvidenceStatus.ERROR,
                notes=f"Error querying ZKP proof records: {str(exc)}",
            )

    @staticmethod
    async def _collect_mna_evidence(
        session: AsyncSession,
        tenant_id: str,
        rule_id: str,
        settings: Settings,
    ) -> MNAEvidenceSection:
        if not getattr(settings, "mna_due_diligence_enabled", False):
            return MNAEvidenceSection(
                status=EvidenceStatus.DISABLED,
                notes="M&A Compliance Due-Diligence Agent is disabled by configuration (mna_due_diligence_enabled=False).",
            )

        try:
            from app.db.models import MNAComparisonJob

            # Find completed MNA jobs involving this tenant
            query = select(MNAComparisonJob).where(
                (MNAComparisonJob.entity_a_id == tenant_id) | (MNAComparisonJob.entity_b_id == tenant_id),
                MNAComparisonJob.status == "COMPLETED",
            ).order_by(MNAComparisonJob.created_at.desc()).limit(5)

            res = await session.execute(query)
            jobs = res.scalars().all()

            findings: list[MNAFindingItem] = []
            for job in jobs:
                if not job.report_data:
                    continue
                raw_findings = job.report_data.get("findings", [])
                compared_entity = job.entity_b_id if job.entity_a_id == tenant_id else job.entity_a_id
                for f in raw_findings:
                    if f.get("rule_id") == rule_id:
                        findings.append(
                            MNAFindingItem(
                                job_id=job.job_id,
                                compared_entity_id=compared_entity,
                                difference_type=f.get("difference_type", "potential_conflict"),
                                severity=f.get("severity", "medium"),
                                title=f.get("title", ""),
                                description=f.get("description", ""),
                                provenance=f.get("provenance", {}),
                            )
                        )

            return MNAEvidenceSection(
                status=EvidenceStatus.AVAILABLE if findings else EvidenceStatus.NOT_FOUND,
                findings_count=len(findings),
                findings=findings,
            )
        except Exception as exc:
            logger.warning("Failed to query M&A due diligence findings: %s", exc)
            return MNAEvidenceSection(
                status=EvidenceStatus.ERROR,
                notes=f"Error querying M&A findings: {str(exc)}",
            )
