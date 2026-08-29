"""Requirement 1's JSON serialization: the same `ComplianceLogFiling`/
`CollateralReportFiling` models, serialized to SEBI-schema-shaped JSON
and validated against a hand-authored JSON Schema
(app/regulatory_filing/schemas/*.schema.json) via the `jsonschema`
library -- a real, executable correctness check, not just "Pydantic
says the types match" (Pydantic's own validation happened already, when
the model was constructed; this additionally proves the SERIALIZED
JSON's shape matches an independently-authored schema document, the way
a regulator's own intake validator would check it).
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from app.regulatory_filing.schemas import CollateralReportFiling, ComplianceLogFiling

_SCHEMAS_DIR = Path(__file__).parent / "schemas"


def serialize_compliance_log_json(filing: ComplianceLogFiling) -> bytes:
    return filing.model_dump_json(indent=2).encode("utf-8")


def serialize_daily_collateral_json(filing: CollateralReportFiling) -> bytes:
    return filing.model_dump_json(indent=2).encode("utf-8")


class JsonSchemaValidationError(ValueError):
    pass


def validate_json(json_bytes: bytes, schema_filename: str) -> None:
    """Raises JsonSchemaValidationError with jsonschema's own diagnostic
    on failure; returns None on success. `schema_filename` is one of
    "compliance_log_v1.schema.json" / "daily_collateral_v1.schema.json"."""
    schema = json.loads((_SCHEMAS_DIR / schema_filename).read_text(encoding="utf-8"))
    document = json.loads(json_bytes)
    try:
        jsonschema.validate(instance=document, schema=schema)
    except jsonschema.ValidationError as exc:
        raise JsonSchemaValidationError(str(exc)) from exc
