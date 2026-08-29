"""Tests for the legal knowledge graph pipeline (app.graph): penalty
detection, cross-reference extraction, the sync pipeline's Cypher
parameter-building (verified against a fake Neo4j session that records
every call), conflict-detection result parsing, and the visualization
builder. No live Neo4j instance is required or used -- see each test
class's docstring for what it actually exercises.
"""
from __future__ import annotations

import pytest

from app.agents.schemas import (
    AuditedComplianceRule,
    AuditVerdict,
    ComparisonOperator,
    ComplianceRuleAudit,
    ExtractedComplianceRule,
    NumericalThreshold,
    ObligationType,
    TargetEntity,
)
from app.graph.conflict_detection import ThresholdConflict, detect_threshold_conflicts
from app.graph.penalty_detector import detect_penalty
from app.graph.reference_extractor import extract_referenced_clause_numbers
from app.graph.sync import sync_audited_rule_to_graph
from app.graph.visualization import build_visualization_from_records
from app.regulatory.taxonomy import Regulator


class TestPenaltyDetector:
    def test_clause_with_no_penalty_language_returns_none(self) -> None:
        text = "Every stock broker shall maintain upfront margin of not less than 20% of the transaction value."
        assert detect_penalty(text) is None

    def test_clause_with_penalty_language_is_detected(self) -> None:
        text = (
            "Every stock broker shall maintain upfront margin of not less than 20%. Failure to comply shall "
            "attract a monetary penalty of INR 1,00,000 per day of default, or suspension of registration."
        )
        result = detect_penalty(text)
        assert result is not None
        assert "penalty" in result.description.lower()
        assert result.amount_text is not None and "1,00,000" in result.amount_text
        assert "suspension of registration" in result.basis_text


class TestReferenceExtractor:
    def test_extracts_other_clause_numbers_excluding_own(self) -> None:
        text = "The limits specified in clause 4.1 shall apply, read with the conditions in clause 3.2.1 and this clause 5.1."
        refs = extract_referenced_clause_numbers(text, own_clause_number="5.1")
        assert refs == ["4.1", "3.2.1"]

    def test_no_references_returns_empty_list(self) -> None:
        text = "Every stock broker shall maintain upfront margin of not less than 20%."
        assert extract_referenced_clause_numbers(text, own_clause_number="4.2.b") == []


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, query: str, **params):
        self.calls.append((query, params))
        return _FakeResult([])


class _FakeResult:
    def __init__(self, records: list[dict]) -> None:
        self._records = records

    async def data(self) -> list[dict]:
        return self._records

    async def single(self):
        return self._records[0] if self._records else None


def _approved_margin_rule(regulator: Regulator = Regulator.SEBI, domain: str = "broking") -> AuditedComplianceRule:
    rule = ExtractedComplianceRule(
        rule_id="a" * 64 + ":4.2.b", source_chunk_id="c1", source_sha256="a" * 64,
        circular_number="SEBI/HO/MIRSD/DOP/CIR/P/2024/100", clause_number="4.2.b",
        target_entities=[TargetEntity(raw_text="stock broker", normalized_entity="Stockbroker", verbatim_evidence="stock broker")],
        deterministic_logic=[NumericalThreshold(metric="Upfront Margin", operator=ComparisonOperator.GTE, value=20, unit="%", verbatim_evidence="20%")],
        obligation_type=ObligationType.MANDATORY, extraction_confidence=0.95,
        regulator=regulator, regulatory_domain=domain,
    )
    audit = ComplianceRuleAudit(rule_id=rule.rule_id, verdict=AuditVerdict.APPROVED, fidelity_score=0.98, verified_quote_count=1, unverified_quote_count=0)
    return AuditedComplianceRule(rule=rule, audit=audit)


@pytest.mark.asyncio
class TestSyncPipeline:
    async def test_approved_rule_writes_circular_clause_entity_obligation(self) -> None:
        session = _FakeSession()
        audited = _approved_margin_rule()

        await sync_audited_rule_to_graph(session, audited)

        queries = [q for q, _ in session.calls]
        assert any("MERGE (c:Circular" in q for q in queries)
        assert any("MERGE (cl:Clause)" in q or "MERGE (cl:Clause" in q for q in queries)
        assert any("MERGE (e:Entity" in q for q in queries)
        assert any("MERGE (o:Obligation" in q for q in queries)

        obligation_call = next(p for q, p in session.calls if "MERGE (o:Obligation" in q)
        assert obligation_call["metric_field"] == "upfront_margin_pct"
        assert obligation_call["value"] == 20.0
        assert obligation_call["regulator"] == "sebi"
        assert obligation_call["domain"] == "broking"

    async def test_rejected_rule_is_never_synced(self) -> None:
        session = _FakeSession()
        rule = ExtractedComplianceRule(
            rule_id="a" * 64 + ":1", source_chunk_id="c", source_sha256="a" * 64,
            circular_number="c", clause_number="1", obligation_type=ObligationType.MANDATORY, extraction_confidence=0.2,
        )
        audit = ComplianceRuleAudit(rule_id=rule.rule_id, verdict=AuditVerdict.REJECTED, fidelity_score=0.1, verified_quote_count=0, unverified_quote_count=1)
        audited = AuditedComplianceRule(rule=rule, audit=audit)

        await sync_audited_rule_to_graph(session, audited)
        assert session.calls == []

    async def test_clause_text_enables_penalty_and_reference_sync(self) -> None:
        session = _FakeSession()
        audited = _approved_margin_rule()
        clause_text = (
            "Every stock broker shall maintain upfront margin of not less than 20%, read with clause 3.2.1. "
            "Failure to comply shall attract a monetary penalty of INR 50,000 per day."
        )

        await sync_audited_rule_to_graph(session, audited, clause_text=clause_text)

        queries = [q for q, _ in session.calls]
        assert any("MERGE (p:Penalty" in q for q in queries)
        assert any("MERGE (referenced:Clause" in q for q in queries)

    async def test_no_clause_text_skips_penalty_and_reference_sync(self) -> None:
        session = _FakeSession()
        audited = _approved_margin_rule()

        await sync_audited_rule_to_graph(session, audited)

        queries = [q for q, _ in session.calls]
        assert not any("Penalty" in q for q in queries)
        assert not any("referenced:Clause" in q for q in queries)

    async def test_supersession_auto_detection_disabled_by_default(self) -> None:
        session = _FakeSession()
        audited = _approved_margin_rule()
        clause_text = "Clause 4.2.b hereby supersedes Clause 1.1 of the Master Circular SEBI/HO/MIRSD/2020/01."

        await sync_audited_rule_to_graph(session, audited, clause_text=clause_text)

        queries = [q for q, _ in session.calls]
        assert not any("SUPERSEDES" in q for q in queries)

    async def test_supersession_auto_detection_writes_flagged_edge_when_enabled(self) -> None:
        from app.config import get_settings

        session = _FakeSession()
        audited = _approved_margin_rule()
        clause_text = "Clause 4.2.b hereby supersedes Clause 1.1 of the Master Circular SEBI/HO/MIRSD/2020/01."
        settings = get_settings().model_copy(update={"supersession_auto_detection_enabled": True})

        await sync_audited_rule_to_graph(session, audited, clause_text=clause_text, settings=settings)

        queries_and_params = session.calls
        edge_call = next((p for q, p in queries_and_params if "MERGE (cl)-[r:SUPERSEDES]->(target)" in q), None)
        assert edge_call is not None
        assert edge_call["confidence"] == 0.8
        assert "SEBI/HO/MIRSD/2020/01" in edge_call["basis_text"]

        target_stub_call = next(p for q, p in queries_and_params if "MERGE (target:Clause" in q)
        assert target_stub_call["target_clause_number_value"] == "1.1"
        assert target_stub_call["target_circular_number"] == "SEBI/HO/MIRSD/2020/01"


@pytest.mark.asyncio
class TestConflictDetection:
    async def test_detects_margin_threshold_conflict_matching_requirement_example(self) -> None:
        """Requirement 2's literal example: Circular A requiring 20%
        margin vs. Master Circular B specifying 15% for the same asset
        class."""
        record = {
            "obligation_a_id": "ruleA:0", "obligation_b_id": "ruleB:0", "entity": "Stockbroker",
            "metric_field": "upfront_margin_pct", "metric": "Upfront Margin",
            "circular_a": "SEBI/HO/MIRSD/1/2024/1", "clause_a": "4.2.b", "value_a": 20.0, "operator": ">=", "unit": "%",
            "circular_b": "SEBI/Master-Circular/2025/1", "clause_b": "3.1", "value_b": 15.0,
            "delta_value": 5.0, "delta_pct": 25.0,
        }

        class _Session:
            async def run(self, query, **params):
                return _FakeResult([record])

        conflicts = await detect_threshold_conflicts(_Session())
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert isinstance(conflict, ThresholdConflict)
        assert conflict.value_a == 20.0
        assert conflict.value_b == 15.0
        assert conflict.delta_pct == 25.0
        assert conflict.entity == "Stockbroker"


class TestVisualizationBuilder:
    class _FakeNode:
        def __init__(self, labels, props):
            self.labels = labels
            self._props = props

        def items(self):
            return self._props.items()

    class _FakeRel:
        def __init__(self, type_, start, end, props=None):
            self.type = type_
            self.start_node = start
            self.end_node = end
            self._props = props or {}

        def items(self):
            return self._props.items()

    def test_builds_nodes_and_edges_from_records(self) -> None:
        circular = self._FakeNode(["Circular"], {"circular_number": "SEBI/HO/1/2024/1"})
        clause = self._FakeNode(["Clause"], {"clause_id": "a" * 64 + ":4.2.b"})
        contains = self._FakeRel("CONTAINS", circular, clause)

        records = [{"nodes": [circular, clause], "relationships": [contains]}]
        viz = build_visualization_from_records(records)

        assert len(viz.nodes) == 2
        assert len(viz.edges) == 1
        assert viz.edges[0].type == "CONTAINS"
        assert viz.edges[0].source == "Circular:SEBI/HO/1/2024/1"
        assert viz.edges[0].target == f"Clause:{'a' * 64}:4.2.b"

    def test_duplicate_edges_across_records_are_deduplicated(self) -> None:
        circular = self._FakeNode(["Circular"], {"circular_number": "SEBI/HO/1/2024/1"})
        clause = self._FakeNode(["Clause"], {"clause_id": "x"})
        contains = self._FakeRel("CONTAINS", circular, clause)

        records = [
            {"nodes": [circular, clause], "relationships": [contains]},
            {"nodes": [circular, clause], "relationships": [contains]},
        ]
        viz = build_visualization_from_records(records)
        assert len(viz.edges) == 1

    def test_empty_records_produce_empty_visualization(self) -> None:
        viz = build_visualization_from_records([])
        assert viz.nodes == []
        assert viz.edges == []
