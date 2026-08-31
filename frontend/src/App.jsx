import { useEffect, useState } from "react";
import Sidebar from "./components/layout/Sidebar";
import TopBar from "./components/layout/TopBar";
import LandingPage from "./components/landing/LandingPage";
import AuthModal from "./components/auth/AuthModal";
import PipelineTracker from "./components/pipeline/PipelineTracker";
import ClauseSplitView from "./components/splitview/ClauseSplitView";
import PolicyPlayground from "./components/playground/PolicyPlayground";
import HITLDashboard from "./components/hitl/HITLDashboard";
import AuditVault from "./components/vault/AuditVault";
import { parseAndIndexCircular } from "./api/ingestionApi";
import { login as loginRequest, signup as signupRequest, decodeToken, isTokenExpired } from "./api/authApi";
import {
  clauses,
  hitlCases as initialHitlCases,
  ledgerFeed,
  pipelineRuns,
} from "./mock/mockData";

// Bearer token issued by this backend's own POST /v1/auth/login
// (app/api/auth_routes.py) -- persisted across reloads so the user isn't
// logged out on every refresh (mirrors the old Auth0 SDK's
// cacheLocation="localstorage" behavior, just handled ourselves now).
const TOKEN_STORAGE_KEY = "regengine_access_token";

// "Remember me" (AuthModal) decides which of these two a login writes to:
// localStorage survives browser restarts, sessionStorage clears when the
// tab closes. Read order matters -- localStorage first, since a token
// there is meant to persist even if this is also a fresh tab.
function loadStoredToken() {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY) || sessionStorage.getItem(TOKEN_STORAGE_KEY);
  if (!token) return null;
  const claims = decodeToken(token);
  if (!claims || isTokenExpired(claims)) {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    return null;
  }
  return { token, claims };
}

export default function App() {
  const [session, setSession] = useState(loadStoredToken);
  const [authLoading, setAuthLoading] = useState(false);
  const [authError, setAuthError] = useState(null);
  const [authModal, setAuthModal] = useState({ open: false, mode: "login" });

  const openAuthModal = (mode) => {
    setAuthError(null);
    setAuthModal({ open: true, mode });
  };
  const closeAuthModal = () => setAuthModal((prev) => ({ ...prev, open: false }));

  const isAuthenticated = Boolean(session);
  const user = session?.claims
    ? { email: session.claims.sub, name: session.claims.sub, roles: session.claims.roles }
    : null;

  // Log a session out on its own once its JWT expires, instead of letting
  // API calls start silently 401ing.
  useEffect(() => {
    if (!session?.claims?.exp) return;
    const msRemaining = session.claims.exp * 1000 - Date.now();
    if (msRemaining <= 0) return;
    const timer = setTimeout(() => {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
      setSession(null);
    }, msRemaining);
    return () => clearTimeout(timer);
  }, [session]);

  const handleLogin = async (email, password, rememberMe = true) => {
    setAuthLoading(true);
    setAuthError(null);
    try {
      const result = await loginRequest(email, password);
      const store = rememberMe ? localStorage : sessionStorage;
      store.setItem(TOKEN_STORAGE_KEY, result.access_token);
      setSession({ token: result.access_token, claims: decodeToken(result.access_token) });
      // isAuthenticated flips on the next render, at which point the
      // landing page (and this modal along with it) stops rendering --
      // no explicit close needed on success.
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setAuthLoading(false);
    }
  };

  // AuthModal's signup form also collects fullName/orgType, but POST
  // /v1/auth/signup (app.security.models.LoginRequest) only takes
  // email+password today -- see AuthModal's own comment at the call site.
  const handleSignup = async ({ email, password }) => {
    setAuthLoading(true);
    setAuthError(null);
    try {
      await signupRequest(email, password);
      return true;
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Sign up failed.");
      return false;
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    setSession(null);
    setAuthModal({ open: false, mode: "login" });
  };

  const [activeView, setActiveView] = useState("pipeline");
  const [hitlCases, setHitlCases] = useState(initialHitlCases);
  const [uploadState, setUploadState] = useState("idle"); // idle | uploading | success | error
  const [uploadResult, setUploadResult] = useState(null);
  const [uploadError, setUploadError] = useState(null);

  const handleUpload = async (file) => {
    setUploadState("uploading");
    setUploadError(null);
    setUploadResult(null);
    try {
      if (!session?.token) {
        throw new Error("Not logged in. Log in first.");
      }
      const result = await parseAndIndexCircular(file, { accessToken: session.token });
      setUploadResult({ filename: file.name, ...result });
      setUploadState("success");
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed.");
      setUploadState("error");
    }
  };

  const resolveHitlCase = (caseId, status, notes) => {
    setHitlCases((prev) =>
      prev.map((c) =>
        c.caseId === caseId
          ? {
              ...c,
              status,
              notes: notes || c.notes,
              resolvedBy: "you@compliance",
              resolvedAt: new Date().toISOString(),
            }
          : c,
      ),
    );
  };

  // Requirement 3's "Submit for HITL Review" action. This mock handler
  // appends locally, matching this codebase's existing convention (see
  // mock/mockData.js's module comment) of shaping mock state exactly
  // like the real backend contract so a real integration is a drop-in
  // swap -- src/api/playgroundApi.js's `submitForHitlReview` documents
  // the intended REST contract this would call instead.
  const submitPlaygroundDraftForReview = async (draft) => {
    await new Promise((resolve) => setTimeout(resolve, 400)); // simulated network latency
    setHitlCases((prev) => [
      {
        caseId: `hitl-pg-${Date.now()}`,
        kind: "playground",
        ruleId: draft.ruleId,
        clauseNumber: draft.clauseNumber,
        circularNumber: draft.circularNumber,
        description: draft.lastEvaluation
          ? `Manually edited in the Policy Playground. Last local evaluation: ${draft.lastEvaluation.allow ? "ALLOW" : "DENY"} (engine: ${draft.lastEvaluation.engine}).`
          : "Manually edited in the Policy Playground.",
        editedCode: draft.editedRego || JSON.stringify(draft.editedJsonLogic, null, 2),
        flaggedAt: new Date().toISOString(),
        status: "pending",
      },
      ...prev,
    ]);
  };

  const pendingHitlCount = hitlCases.filter(
    (c) => c.status === "pending",
  ).length;

  if (!isAuthenticated) {
    return (
      <>
        <LandingPage onOpenAuth={openAuthModal} />
        <AuthModal
          isOpen={authModal.open}
          initialMode={authModal.mode}
          onClose={closeAuthModal}
          onLogin={handleLogin}
          onSignup={handleSignup}
          isLoading={authLoading}
          error={authError}
        />
      </>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-ink-950">
      <Sidebar
        activeView={activeView}
        onNavigate={setActiveView}
        pendingHitlCount={pendingHitlCount}
      />
      <div className="flex flex-1 flex-col overflow-hidden">
        <TopBar
          activeView={activeView}
          isAuthenticated={isAuthenticated}
          user={user}
          onLogout={handleLogout}
        />
        <main className="flex-1 overflow-y-auto scrollbar-thin p-4">
          {activeView === "pipeline" && (
            <PipelineTracker
              runs={pipelineRuns}
              onUpload={handleUpload}
              uploadState={uploadState}
              uploadResult={uploadResult}
              uploadError={uploadError}
            />
          )}
          {activeView === "splitview" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <ClauseSplitView clauses={clauses} />
            </div>
          )}
          {activeView === "playground" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <PolicyPlayground clauses={clauses} onSubmitForReview={submitPlaygroundDraftForReview} />
            </div>
          )}
          {activeView === "hitl" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <HITLDashboard
                cases={hitlCases}
                onResolveCase={resolveHitlCase}
              />
            </div>
          )}
          {activeView === "vault" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <AuditVault initialFeed={ledgerFeed} />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
