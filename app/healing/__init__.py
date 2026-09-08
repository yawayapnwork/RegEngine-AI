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

Self-healing policy repair loop (app.healing): intercepts an OPA
compile/publish failure or a JSON-Logic runtime crash
(app.healing.detectors), hands the failed Rego, its error/stack trace,
and the original legal clause text to the Policy Repair Agent
(app.healing.repair_agent, prompts in app.healing.repair_prompts), and
runs a bounded, tracked (app.healing.tracking) retry loop
(app.healing.orchestrator.SelfHealingLoop) that validates every repair
against isolated test cases before it is ever handed to the existing
HITL/publish pipeline.
"""
