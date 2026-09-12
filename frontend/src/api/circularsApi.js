import { API_BASE_URL as DEFAULT_BASE_URL, fetchWithTimeout, UPLOAD_TIMEOUT_MS } from "./config";

export class CircularsApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "CircularsApiError";
    this.status = status;
  }
}

function authHeaders(accessToken) {
  const headers = {};
  if (accessToken) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }
  return headers;
}

export async function processCircularE2E(file, { accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetchWithTimeout(
    new URL(`${baseUrl}/v1/circulars/process-e2e`, window.location.origin),
    { method: "POST", headers: authHeaders(accessToken), body: formData, timeout: UPLOAD_TIMEOUT_MS },
  );

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new CircularsApiError(
      body?.detail || `Processing failed with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function getCircularStatus(circularId, { accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/circulars/${circularId}/status`, window.location.origin), {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new CircularsApiError(
      body?.detail || `Status fetch failed with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function getCircularDetails(circularId, { accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/circulars/${circularId}/details`, window.location.origin), {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new CircularsApiError(
      body?.detail || `Details fetch failed with status ${response.status}.`,
      response.status
    );
  }
  return body;
}

export async function listCirculars({ accessToken, baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetchWithTimeout(new URL(`${baseUrl}/v1/circulars`, window.location.origin), {
    method: "GET",
    headers: authHeaders(accessToken),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new CircularsApiError(
      body?.detail || `Listing circulars failed with status ${response.status}.`,
      response.status
    );
  }
  return body;
}
