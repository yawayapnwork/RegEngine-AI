"""Automated grievance escalation agent: when a broker's non-compliance
is SYSTEMIC (the same broker breaching the same rule repeatedly within
a rolling window, not one isolated failure), assembles an evidence
package (the SEBI clause hash, the transaction payload, and a SHA-256
audit-ledger chain proof for that specific transaction) and files it as
a SEBI SCORES grievance record, then polls SCORES for status updates
and feeds resolution timelines into the internal HITL compliance
dashboard.

IMPORTANT, read before treating `app.grievance_escalation.scores_client`
as a verified live integration: this codebase's author does not have
confirmed, verified knowledge of SEBI SCORES 2.0's actual published
REST API contract (exact endpoint paths, field names, authentication
flow). `app.grievance_escalation.schemas` models a best-effort,
clearly-labeled interface built from SCORES' publicly documented
GRIEVANCE WORKFLOW shape (complainant/respondent identification,
grievance category/sub-category, a free-text description, and
supporting evidence) -- not a byte-for-byte transcription of a real API
spec. `scores_api_base_url` defaults to `None`, and
`app.grievance_escalation.scores_client.ScoresApiClient` refuses to
submit anything against an unconfigured base URL rather than silently
guessing at a real SEBI endpoint. Before pointing this at a live
SCORES environment, reconcile `schemas.py`'s field mapping against
SEBI's actual current API documentation (available to registered
intermediaries) and update it there -- every other module in this
package (systemic-failure detection, evidence assembly, the queue,
Celery tasks, the dashboard integration) is real, working automation
that does not depend on SCORES' exact wire format being correct today.

Gated behind `settings.grievance_escalation_enabled` (default False).
"""
