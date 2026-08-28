"""Tamper-evident audit ledger for RegEngine AI compliance evaluations:
PostgreSQL append-only storage with a SHA-256 hash chain over every row,
in the spirit of AWS QLDB's journal-block model (see sql/ledger_schema.sql
for the immutability enforcement layers)."""
