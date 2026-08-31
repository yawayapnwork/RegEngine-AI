// Real fetch client for POST /v1/circulars/parse-and-index (app/api/routes.py).
// Auth: Compliance_Officer or System_Admin bearer token -- NOT a tenant
// header. SEBI circulars are shared regulatory baseline data, not
// per-tenant content, so this endpoint has no tenant scoping at all; see
// app/api/routes.py's `_require_ingestion_role`.
//
// multipart/form-data is required (the backend declares
// `file: UploadFile = File(...)`), so the body must be a FormData
// instance and Content-Type must be left for the browser to set (it adds
// the multipart boundary itself -- setting it manually breaks parsing).

const DEFAULT_BASE_URL = import.meta.env?.VITE_API_BASE_URL || "";

export class IngestionApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "IngestionApiError";
    this.status = status;
    this.body = body;
  }
}

export async function parseAndIndexCircular(
  file,
  { recreateCollection = false, baseUrl = DEFAULT_BASE_URL, accessToken } = {},
) {
  const formData = new FormData();
  formData.append("file", file, file.name);

  const url = new URL(`${baseUrl}/v1/circulars/parse-and-index`, window.location.origin);
  url.searchParams.set("recreate_collection", String(recreateCollection));

  const response = await fetch(url, {
    method: "POST",
    headers: {
      // No Content-Type here -- fetch sets `multipart/form-data;
      // boundary=...` automatically from the FormData body.
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: formData,
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new IngestionApiError(body?.detail || `Upload failed with status ${response.status}.`, response.status, body);
  }
  return body;
}

// Async upload flow -- POST /v1/ingestion/uploads (app/api/ingestion_routes.py).
// Unlike parseAndIndexCircular above, this returns immediately (202) with a
// job_id: the actual hi-res OCR + embedding pipeline runs in a Celery
// worker, since it can run far longer than any HTTP proxy's request
// timeout would tolerate. Callers poll getUploadJobStatus for progress.
export async function createUploadJob(file, { baseUrl = DEFAULT_BASE_URL, accessToken } = {}) {
  const formData = new FormData();
  formData.append("file", file, file.name);

  const url = new URL(`${baseUrl}/v1/ingestion/uploads`, window.location.origin);

  const response = await fetch(url, {
    method: "POST",
    headers: {
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: formData,
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new IngestionApiError(body?.detail || `Upload failed with status ${response.status}.`, response.status, body);
  }
  return body; // { job_id, status }
}

export async function getUploadJobStatus(jobId, { baseUrl = DEFAULT_BASE_URL, accessToken } = {}) {
  const url = new URL(`${baseUrl}/v1/ingestion/uploads/${jobId}`, window.location.origin);

  const response = await fetch(url, {
    headers: {
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new IngestionApiError(body?.detail || `Status check failed with status ${response.status}.`, response.status, body);
  }
  return body; // { job_id, filename, status, chunks_indexed, error_message }
}
