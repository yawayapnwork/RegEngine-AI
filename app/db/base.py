"""Shared declarative base and naming convention for every ORM-mapped table
in the schema (circulars, clauses, compiled_rules, hitl_reviews) plus the
Core-mapped `compliance_audit_ledger` table from `app.ledger.models`, which
binds to this same `Base.metadata` rather than defining its own.

A fixed naming convention is required for Alembic's `--autogenerate` to
produce stable, deterministic constraint/index names across runs instead of
SQLAlchemy's default anonymous `%(constraint_name)s` labels, which differ
between dialects and even between runs.
"""
from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
