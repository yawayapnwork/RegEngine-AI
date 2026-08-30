import { useState } from "react";
import { useAuth0 } from "@auth0/auth0-react";
import Sidebar from "./components/layout/Sidebar";
import TopBar from "./components/layout/TopBar";
import PipelineTracker from "./components/pipeline/PipelineTracker";
import ClauseSplitView from "./components/splitview/ClauseSplitView";
import PolicyPlayground from "./components/playground/PolicyPlayground";
import HITLDashboard from "./components/hitl/HITLDashboard";
import AuditVault from "./components/vault/AuditVault";
import { parseAndIndexCircular } from "./api/ingestionApi";
import {
  clauses,
  hitlCases as initialHitlCases,
  ledgerFeed,
  pipelineRuns,
} from "./mock/mockData";

// LOCAL-DEV-ONLY fallback -- see frontend/.env.example. Never set this in
// a deployed environment: it bakes a static Bearer token into the public
// JS bundle. Real auth is Auth0 (below); this only exists so `npm run dev`
// works without clicking through a login on every restart.
const DEV_ACCESS_TOKEN = import.meta.env?.VITE_DEV_ACCESS_TOKEN || undefined;
const AUTH0_AUDIENCE = import.meta.env?.VITE_AUTH0_AUDIENCE;

export default function App({ auth0Configured = true }) {
  const {
    isAuthenticated,
    isLoading: authLoading,
    loginWithRedirect,
    logout,
    getAccessTokenSilently,
    user,
  } = useAuth0();

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
      let accessToken = DEV_ACCESS_TOKEN;
      if (auth0Configured && isAuthenticated) {
        // Auth0 SDK handles caching + silent refresh internally -- this
        // is cheap to call on every upload, never a network round trip
        // unless the cached token is actually expired.
        accessToken = await getAccessTokenSilently(
          AUTH0_AUDIENCE ? { authorizationParams: { audience: AUTH0_AUDIENCE } } : undefined,
        );
      } else if (auth0Configured && !accessToken) {
        throw new Error("Not logged in. Click \"Log in\" in the top bar first.");
      }
      const result = await parseAndIndexCircular(file, { accessToken });
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
          auth0Configured={auth0Configured}
          isAuthenticated={isAuthenticated}
          isLoading={authLoading}
          user={user}
          onLogin={() => loginWithRedirect()}
          onLogout={() => logout({ logoutParams: { returnTo: window.location.origin } })}
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
