"""[FROZEN / NON-MVP EXPERIMENTAL SUBSYSTEM]
===============================================================================
Status: Frozen / Non-MVP Experimental
Core MVP Pipeline: Regulatory Document -> Extraction -> Clause Interpretation
                  -> Canonical Facts -> Policy Compilation -> HITL Review
                  -> Policy Activation -> OPA Evaluation -> Evidence Ledger

This subsystem is preserved for future architectural extensions but is outside
the active regulatory-compliance MVP. It is not imported or required by the
core execution, compilation, ingestion, or ledger pipeline.
===============================================================================

Zero-knowledge proof verification (app.zkp): lets a broker prove
`collected_margin >= required_margin` for a trade -- see
zk/circuits/margin_compliance.circom -- without ever sending the actual
margin amount or client account identifier to RegEngine. See
app.zkp.groth16_verifier for the server-side verification math and
app.api.zkp_routes for the endpoint that verifies a submitted proof and
writes it to the compliance_audit_ledger.
"""
