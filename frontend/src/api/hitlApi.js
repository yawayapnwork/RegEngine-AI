import { API_BASE_URL as DEFAULT_BASE_URL, fetchWithTimeout } from "./config";

export class HitlApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "HitlApiError";
    this.status = status;
  }
}

function authHeaders(accessToken) {
  const headers = { "Content-Type": "application/json" };
  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }
  return headers;
}

export async function listHitlReviews({ statusFilter, accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const url = new URL(`${baseUrl}/v1/hitl-reviews`, window.location.origin);
  if (statusFilter) {
    url.searchParams.set("status_filter", statusFilter);
  }

  const response = await fetchWithTimeout(url, {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new HitlApiError(
      body?.detail || `Failed to fetch HITL reviews with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function getHitlReview(reviewId, { accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/hitl-reviews/${reviewId}`, window.location.origin), {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new HitlApiError(
      body?.detail || `Failed to fetch review with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function approveHitlReview(reviewId, { notes = "", accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/hitl-reviews/${reviewId}/approve`, window.location.origin), {
    method: "POST",
    headers: authHeaders(accessToken),
    body: JSON.stringify({ notes }),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new HitlApiError(
      body?.detail || `Failed to approve review with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function rejectHitlReview(reviewId, { notes = "", accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/hitl-reviews/${reviewId}/reject`, window.location.origin), {
    method: "POST",
    headers: authHeaders(accessToken),
    body: JSON.stringify({ notes }),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new HitlApiError(
      body?.detail || `Failed to reject review with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function getHitlReviewEvidence(reviewId, { accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/hitl-reviews/${reviewId}/evidence`, window.location.origin), {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new HitlApiError(
      body?.detail || `Failed to fetch review evidence with status ${response.status}.`,
      response.status
    );
  }
  return body;
}
