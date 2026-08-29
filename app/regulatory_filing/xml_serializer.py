"""Requirement 1's XML serialization: `ComplianceLogFiling`/
`CollateralReportFiling` -> a SEBI-e-filing-shaped XML document,
validated against a hand-authored XSD (app/regulatory_filing/schemas/*.xsd)
via `lxml` (already a dependency -- see requirements.txt's SEBI RSS/HTML
ingestion comment) -- a real, executable correctness check that the
serializer's output actually conforms to a declared schema, independent
of the exact real SEBI XSD this stands in for (see schemas.py's module
docstring on why that specific XSD isn't a fixed, fetchable artifact).

Uses the stdlib `xml.etree.ElementTree` for BUILDING the document (no
new dependency for the write path) and `lxml.etree` only for schema
VALIDATION, where its C-backed libxml2 XMLSchema support is what
actually makes real XSD validation possible without hand-rolling one.
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from lxml import etree as lxml_etree

from app.regulatory_filing.schemas import CollateralReportFiling, ComplianceLogFiling, FilingHeader

_NAMESPACE = "urn:regengine:sebi-filing:v1"
_SCHEMAS_DIR = Path(__file__).parent / "schemas"

ET.register_namespace("", _NAMESPACE)


def _q(tag: str) -> str:
    return f"{{{_NAMESPACE}}}{tag}"


def _header_element(header: FilingHeader) -> ET.Element:
    el = ET.Element(_q("Header"))
    ET.SubElement(el, _q("FilingId")).text = header.filing_id
    ET.SubElement(el, _q("ReportingEntityCode")).text = header.reporting_entity_code
    ET.SubElement(el, _q("Target")).text = header.target.value
    ET.SubElement(el, _q("PeriodStart")).text = header.period_start.isoformat()
    ET.SubElement(el, _q("PeriodEnd")).text = header.period_end.isoformat()
    ET.SubElement(el, _q("GeneratedAt")).text = header.generated_at.isoformat()
    ET.SubElement(el, _q("RecordCount")).text = str(header.record_count)
    ET.SubElement(el, _q("ContentSHA256")).text = header.content_sha256
    return el


def serialize_compliance_log_xml(filing: ComplianceLogFiling) -> bytes:
    root = ET.Element(_q("RegulatoryFiling"), {"filingType": filing.header.filing_type.value})
    root.append(_header_element(filing.header))
    records_el = ET.SubElement(root, _q("ComplianceLogRecords"))
    for record in filing.records:
        record_el = ET.SubElement(records_el, _q("Record"))
        ET.SubElement(record_el, _q("SequenceNum")).text = str(record.sequence_num)
        ET.SubElement(record_el, _q("BrokerId")).text = record.broker_id
        ET.SubElement(record_el, _q("TransactionId")).text = record.transaction_id
        ET.SubElement(record_el, _q("EvaluatedAt")).text = record.evaluated_at.isoformat()
        ET.SubElement(record_el, _q("CircularId")).text = record.circular_id
        ET.SubElement(record_el, _q("ClauseHash")).text = record.clause_hash
        ET.SubElement(record_el, _q("SectionReference")).text = record.section_reference
        ET.SubElement(record_el, _q("RuleId")).text = record.rule_id
        ET.SubElement(record_el, _q("EvaluationResult")).text = record.evaluation_result
        ET.SubElement(record_el, _q("LedgerCurrentHash")).text = record.ledger_current_hash
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def serialize_daily_collateral_xml(filing: CollateralReportFiling) -> bytes:
    root = ET.Element(_q("RegulatoryFiling"), {"filingType": filing.header.filing_type.value})
    root.append(_header_element(filing.header))
    records_el = ET.SubElement(root, _q("CollateralMetricRecords"))
    for record in filing.records:
        record_el = ET.SubElement(records_el, _q("Record"))
        ET.SubElement(record_el, _q("ReportDate")).text = record.report_date.isoformat()
        ET.SubElement(record_el, _q("BrokerId")).text = record.broker_id
        ET.SubElement(record_el, _q("TransactionsEvaluated")).text = str(record.transactions_evaluated)
        ET.SubElement(record_el, _q("TransactionsPassed")).text = str(record.transactions_passed)
        ET.SubElement(record_el, _q("TransactionsFailed")).text = str(record.transactions_failed)
        ET.SubElement(record_el, _q("TransactionsFlaggedHitl")).text = str(record.transactions_flagged_hitl)
        if record.avg_upfront_margin_pct is not None:
            ET.SubElement(record_el, _q("AvgUpfrontMarginPct")).text = repr(record.avg_upfront_margin_pct)
        if record.min_upfront_margin_pct is not None:
            ET.SubElement(record_el, _q("MinUpfrontMarginPct")).text = repr(record.min_upfront_margin_pct)
        ET.SubElement(record_el, _q("ShortfallCount")).text = str(record.shortfall_count)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


class XmlSchemaValidationError(ValueError):
    pass


def validate_xml(xml_bytes: bytes, xsd_filename: str) -> None:
    """Raises XmlSchemaValidationError with libxml2's own diagnostic log
    on failure; returns None (no exception) on success. `xsd_filename`
    is one of "compliance_log_v1.xsd" / "daily_collateral_v1.xsd"."""
    schema_doc = lxml_etree.parse(str(_SCHEMAS_DIR / xsd_filename))
    schema = lxml_etree.XMLSchema(schema_doc)
    doc = lxml_etree.fromstring(xml_bytes)
    if not schema.validate(doc):
        raise XmlSchemaValidationError(str(schema.error_log))
