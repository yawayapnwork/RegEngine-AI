"""Board-Level Governance & Kill-Switch Control Engine, built to the
SEBI AI/ML Framework's core accountability requirements for a regulated
intermediary's use of AI/ML in its operations:

  - app.governance.kill_switch / .middleware / .fallback -- Requirement 1:
    a global and tenant-specific execution halt (`kill_switch()`) that
    revokes API access (KillSwitchMiddleware, mounted on every request),
    stops the live transaction-evaluation path from calling out to OPA
    (the "freezes execution queues" half), and routes transactions to
    the existing HITL manual-review queue instead (the "falls back to
    manual human workflows" half) -- reusing
    app.execution.hitl_queue.HITLQueue rather than inventing a second
    queue.
  - app.governance.inventory -- Requirement 2: a durable registry
    (app.db.models.AgentInventory) of every deployed AI/ML agent this
    platform runs, its model weight version, business domain, whether
    it participates in a critical operation, and a NAMED human
    compliance officer owner.
  - app.governance.reporting -- Requirement 3: periodic governance audit
    reports, built by composing the EXISTING analytics/HITL reporting
    pipelines (app.analytics.aggregator.ComplianceAggregator,
    app.reporting.data_collector.collect_hitl_approvals) with the new
    kill-switch drill-test history (app.db.models.KillSwitchEvent) --
    never a parallel reporting pipeline.
"""
