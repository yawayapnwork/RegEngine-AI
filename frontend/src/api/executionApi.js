import { API_BASE_URL as DEFAULT_BASE_URL, fetchWithTimeout } from "./config";

export class ExecutionApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ExecutionApiError";
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

export async function evaluateTransaction(payload, { accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/execution/transactions/evaluate`, window.location.origin), {
    method: "POST",
    headers: authHeaders(accessToken),
    body: JSON.stringify(payload),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ExecutionApiError(
      body?.detail || `Evaluation failed with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function getLedgerEntries({ limit = 50, accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const url = new URL(`${baseUrl}/v1/execution/ledger/entries`, window.location.origin);
  url.searchParams.set("limit", limit);

  const response = await fetchWithTimeout(url, {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ExecutionApiError(
      body?.detail || `Failed to fetch ledger entries with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function verifyLedgerChain({ accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/execution/ledger/verify`, window.location.origin), {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ExecutionApiError(
      body?.detail || `Ledger verification failed with status ${response.status}.`,
      response.status
    );
  }
  return body;
}
