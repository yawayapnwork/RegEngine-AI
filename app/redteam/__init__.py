"""AI Red Team adversarial evaluation pipeline: stress-tests and
hardens app.agents' dual-agent Extraction/Logic-Auditor framework
against prompt injection, jailbreaking, and legal-evasion techniques
embedded in ingested SEBI documents.

  - app.redteam.attack_generator -- crafts real adversarial PDFs with
    hidden injection payloads (Requirement 1).
  - app.redteam.defense -- input sanitization + prompt boundary
    isolation, wired into app.agents.crew.build_extraction_task as an
    opt-in hardening layer (Requirement 2, settings.redteam_defense_enabled).
  - app.redteam.output_guard -- Guardrails-AI-based structured-output
    enforcement, catching injection leakage into agent output
    (Requirement 2).
  - app.redteam.telemetry -- the "security vault": a durable record of
    every red-team scenario run and its outcome (Requirement 3).
  - app.redteam.benchmark -- the automated benchmark suite tying the
    above together into a measured resistance rate (Requirement 3).
"""
