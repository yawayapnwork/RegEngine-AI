# Audit Service

PostgreSQL, append-only, SHA-256 hash-chained blocks, QLDB-journal-inspired design per ADR-0003.

Every compliance evaluation's block hash, previous-hash link, and
payload digest are durably recorded in PostgreSQL (`compliance_audit_ledger`)
and are independently re-verifiable end to end, with no active dependency
on AWS QLDB or external cloud ledger services.

Run locally: `uvicorn app.main:app --reload --port 8005`
