# Audit Service

Append-only, SHA-256 hash-chained compliance ledger on PostgreSQL --
every compliance evaluation's block hash, previous-hash link, and
payload digest, independently re-verifiable end to end.

Run locally: `uvicorn app.main:app --reload --port 8005`
