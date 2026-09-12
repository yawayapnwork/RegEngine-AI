// Real fetch client for the Policy Playground's "Submit for HITL Review"
// action. Written against the intended REST contract so backend integration
// requires no component changes.
//
// App.jsx handles draft review submissions by updating the in-memory review
// queue, or calling submitForHitlReview(...) when the dedicated endpoint is active.
//
// Intended backend contract (not yet implemented server-side):
//   POST /v1/hitl-reviews/playground-submissions
//   Auth: Compliance_Officer or System_Admin bearer token
//   Body: {
//     clause_id: number,          // real app.db.models.Clause.id this draft is scoped to
//     rule_id: string,
//     edited_rego: string | null,
//     edited_json_logic: object | null,
//     evaluation_summary: string, // e.g. "3/3 test transactions evaluated locally: 2 ALLOW, 1 DENY"
//     notes: string | null,
//   }
//   201 Response: HITLReviewOut (see app/api/hitl_review_routes.py) --
//   the same shape GET /v1/hitl-reviews already returns, so a submitted
//   playground draft shows up in the existing HITL Compliance Review
//   dashboard with no separate UI.

import { API_BASE_URL as DEFAULT_BASE_URL, fetchWithTimeout } from "./config";

export class PlaygroundApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "PlaygroundApiError";
    this.status = status;
    this.body = body;
  }
}

export async function submitForHitlReview(payload, { baseUrl = DEFAULT_BASE_URL, accessToken } = {}) {
  const response = await fetchWithTimeout(`${baseUrl}/v1/hitl-reviews/playground-submissions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: JSON.stringify(payload),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new PlaygroundApiError(body?.detail || `Submission failed with status ${response.status}.`, response.status, body);
  }
  return body;
}
