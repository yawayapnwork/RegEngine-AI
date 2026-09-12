// Shared API configuration for the frontend.

// The deployed backend API. The Vercel frontend (regengine-ai.vercel.app)
// only serves static files -- there is no /v1 backend on that origin -- so
// a production build must target the real API. VITE_API_BASE_URL, when set
// at build time (e.g. for local dev against a different backend, or a
// per-preview override), always wins.
const PROD_API_BASE_URL = "https://regengine-ai.onrender.com";

const envBaseUrl = (import.meta.env?.VITE_API_BASE_URL || "").trim().replace(/\/+$/, "");

export const API_BASE_URL = envBaseUrl || (import.meta.env.PROD ? PROD_API_BASE_URL : "");

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