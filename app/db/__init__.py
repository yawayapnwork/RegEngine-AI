"""Relational schema for RegEngine AI: circulars -> clauses -> compiled_rules,
HITL review records, and the tamper-evident audit ledger (`app.ledger`).

`app.ledger` keeps its own Core-based table (hash-chain writes need direct
control over statement shape — see `app.ledger.models`), but it shares this
package's `Base.metadata` so Alembic autogenerate and `create_all` see the
whole schema as one unit.
"""
