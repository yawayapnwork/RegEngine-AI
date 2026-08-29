"""Automated regulatory filing adapter: packages evaluated compliance
logs and daily collateral metrics into SEBI/MII e-filing schemas
(app.regulatory_filing.xml_serializer / json_serializer), signs them
with a PKI (X.509/PKCS#7, software-key or HSM-backed --
app.regulatory_filing.signing), and submits them via SFTP or a
regulatory portal API with retry/acknowledgment tracking
(app.regulatory_filing.submission, .tasks).

Every stage sources from data this system already produces as the
single source of truth: compliance-log records come from the real
hash-chained `compliance_audit_ledger` (app.ledger.models), never a
parallel copy -- see collateral_aggregator.py.
"""
