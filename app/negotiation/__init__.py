"""Multi-agent negotiation protocol for resolving conflicting
multi-clause compliance requirements during trade execution.

Where `app.execution.evaluator.Evaluator` reduces every compiled
policy's raw OPA outcome with one fixed rule (any violation -> DENY),
this package sits ONE LAYER ABOVE that reduction for transactions whose
matched policies span more than one regulatory sub-domain: specialized
`DomainAgent`s (Margin, Risk Disclosure, Fund Segregation) each cast an
independent, evidence-cited vote, a deterministic weighted-consensus
state machine (`app.negotiation.consensus`) tries to resolve
disagreement across bounded negotiation rounds, and a higher-tier
`ConflictArbiterAgent` (`app.negotiation.arbiter`) either issues a
definitive, clause-cited resolution or escalates to the SAME
`app.execution.hitl_queue.HITLQueue` an ambiguous OPA result already
uses -- one human review queue, not two.

Gated behind `settings.negotiation_enabled` (default False); a
deployment that never enables it is entirely unaffected -- this package
is additive, invoked by a caller choosing to run it on top of
`Evaluator.evaluate_transaction`'s output, not a replacement for it.
"""
