"""Zero-knowledge proof verification (app.zkp): lets a broker prove
`collected_margin >= required_margin` for a trade -- see
zk/circuits/margin_compliance.circom -- without ever sending the actual
margin amount or client account identifier to RegEngine. See
app.zkp.groth16_verifier for the server-side verification math and
app.api.zkp_routes for the endpoint that verifies a submitted proof and
writes it to the compliance_audit_ledger.
"""
