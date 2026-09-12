// Shared API configuration for the frontend.

// The deployed backend API. The Vercel deployment serves BOTH the API and
// this frontend from the same origin: `vercel.json` (repo root) routes
// /v1/... to the FastAPI app in app/main.py and serves the built frontend
// beside it, so a production build calls same-origin /v1/... -- no
// cross-origin request, no CORS. Local dev is likewise same-origin
// (/v1/... proxied to the backend by vite.config.js). VITE_API_BASE_URL,
// when set at build time (e.g. to point a preview at a different backend,
// or a split frontend/API deployment), always wins.
const envBaseUrl = (import.meta.env?.VITE_API_BASE_URL || "").trim().replace(/\/+$/, "");

export const API_BASE_URL = envBaseUrl || "";

// Default and upload request budgets. The E2E circular path (OCR/extraction
// + indexing) is synchronous and can run for minutes on large PDFs, so it
// gets a much larger budget; a cold-sleeping Render instance can also take
// ~1 min to boot, so ordinary requests get 60s before they give up.
const DEFAULT_TIMEOUT_MS = 60000;
const UPLOAD_TIMEOUT_MS = 600000;

// fetch wrapper that aborts after `timeout` ms (defaults to
// DEFAULT_TIMEOUT_MS; pass `timeout` inside the fetch init to override).
// Without a timeout a request to a cold-sleeping/never-responding backend
// would hang forever and leave the pipeline UI stuck in "Loading..." /
// "Upload in progress..." with no way to recover.
export async function fetchWithTimeout(input, init = {}) {
  const { timeout = DEFAULT_TIMEOUT_MS, ...fetchInit } = init;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    return await fetch(input, { ...fetchInit, signal: controller.signal });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeout / 1000)}s.`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export { UPLOAD_TIMEOUT_MS };