// Local (standalone) email/password auth -- POST /v1/auth/login
// (app/api/auth_routes.py). Replaces the old Auth0-hosted login redirect:
// credentials are posted directly to this backend, which verifies them
// against app.security.local_user_store and returns a self-issued JWT.

const DEFAULT_BASE_URL = import.meta.env?.VITE_API_BASE_URL || "";

export class AuthApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "AuthApiError";
    this.status = status;
  }
}

export async function login(email, password, { baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetch(new URL(`${baseUrl}/v1/auth/login`, window.location.origin), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new AuthApiError(body?.detail || `Login failed with status ${response.status}.`, response.status);
  }
  return body; // { access_token, token_type, expires_in, scope }
}

export async function signup(email, password, { baseUrl = DEFAULT_BASE_URL } = {}) {
  const response = await fetch(new URL(`${baseUrl}/v1/auth/signup`, window.location.origin), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new AuthApiError(body?.detail || `Sign up failed with status ${response.status}.`, response.status);
  }
  return body; // { message, user_id }
}

// Decodes a JWT's claims without verifying the signature -- purely for
// client-side display (whoami chip, role-gated UI). The backend is the
// only party that ever trusts these claims; this is not an auth check.
export function decodeToken(token) {
  try {
    const [, payload] = token.split(".");
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(decodeURIComponent(escape(json)));
  } catch {
    return null;
  }
}

export function isTokenExpired(claims) {
  if (!claims?.exp) return false;
  return Date.now() >= claims.exp * 1000;
}
