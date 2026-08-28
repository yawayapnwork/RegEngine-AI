"""FastAPI dependency wiring for the audit ledger."""
from __future__ import annotations

from app.ledger.db import get_ledger_engine
from app.ledger.service import LedgerService


def get_ledger_service() -> LedgerService:
    return LedgerService(get_ledger_engine())
