// Real fetch client for the Policy Playground's "Submit for HITL Review"
// action (Requirement 3). Written against the intended REST contract so
// wiring it in later requires no component changes -- see
// src/mock/mockData.js's own module comment for why this codebase's
// convention is "shape mock state exactly like the real contract now,
// swap the fetch call in later," rather than build components against
// an ad hoc mock shape that would need rewriting.
//
// PolicyPlayground.jsx does not call this module directly in the
// bundled demo -- App.jsx wires a local mock handler
// (`submitPlaygroundDraftForReview`) that appends to `hitlCases` state,
// exactly like every other view's callback props (`onResolveCase`,
// `onUpload`). Swap that handler's body for `submitForHitlReview(...)`
// from this module once a backend endpoint matching this contract
// exists, matching the request/response shapes documented below.
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

const DEFAULT_BASE_URL = import.meta.env?.VITE_API_BASE_URL || "";

export class PlaygroundApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "PlaygroundApiError";
    this.status = status;
    this.body = body;
  }
}

export async function submitForHitlReview(payload, { baseUrl = DEFAULT_BASE_URL, accessToken } = {}) {
  const response = await fetch(`${baseUrl}/v1/hitl-reviews/playground-submissions`, {
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
