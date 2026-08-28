"""Pure hash-chaining primitives — no I/O, no DB — so the cryptographic
core of the ledger can be tested and audited in isolation from Postgres.

Two-hash design, mirroring how QLDB separates a document's own hash from
its position in the journal:

    payload_digest = SHA-256(canonical_json(business_fields))
    current_hash   = SHA-256(previous_hash || payload_digest || sequence_num || evaluated_at)

`payload_digest` alone lets a verifier confirm "this row's business content
matches what was originally hashed" independent of chain position.
`current_hash` additionally binds that content to its exact position in
history (`sequence_num`) and to everything before it (`previous_hash`), so
altering, reordering, or deleting any row changes every `current_hash` from
that point forward — the same property a blockchain or QLDB journal relies
on for tamper evidence.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64

# Fields hashed into payload_digest, in this fixed order. Explicit (not
# "all model fields") so adding a new column to ComplianceEvaluationEvent
# is a deliberate, reviewed decision about what the chain covers, not an
# accident of dict iteration order.
_PAYLOAD_FIELDS = (
    "broker_id",
    "transaction_id",
    "evaluated_at",
    "circular_id",
    "clause_hash",
    "section_reference",
    "rule_id",
    "evaluation_result",
    "hitl_review_id",
    "details",
)


def _iso(value: Any) -> Any:
    """Normalize to a UTC ISO-8601 string. Naive datetimes are treated as
    already-UTC — the app always writes `evaluated_at` as UTC-aware, but
    some drivers (e.g. SQLite, used in tests) drop tzinfo on round-trip;
    without this normalization the same instant would hash differently
    depending on whether it came from a fresh event or a row read back
    from such a driver, which is a correctness bug, not just a test
    inconvenience."""
    if not isinstance(value, dt.datetime):
        return value
    aware = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return aware.astimezone(dt.timezone.utc).isoformat()


def canonical_payload(event: dict[str, Any]) -> str:
    """Deterministic JSON serialization: fixed key order, no whitespace, no
    locale/float-formatting ambiguity (json.dumps with sort_keys is stable
    across Python versions for the JSON-safe types this module ever
    receives)."""
    ordered = {field: _iso(event[field]) for field in _PAYLOAD_FIELDS}
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_payload_digest(event: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(event).encode("utf-8")).hexdigest()


def compute_block_hash(*, previous_hash: str, payload_digest: str, sequence_num: int, evaluated_at: dt.datetime) -> str:
    material = f"{previous_hash}|{payload_digest}|{sequence_num}|{_iso(evaluated_at)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
