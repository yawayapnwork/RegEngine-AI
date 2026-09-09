"""Tamper-evident audit ledger for RegEngine AI compliance evaluations:
PostgreSQL, append-only, SHA-256 hash-chained blocks, QLDB-journal-inspired design per ADR-0003
(see sql/ledger_schema.sql and docs/adr/0003-sha256-hash-chain-audit-log.md
for the immutability enforcement layers)."""
