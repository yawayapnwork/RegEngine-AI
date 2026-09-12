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
import { login as loginRequest, signup as signupRequest, decodeToken, isTokenExpired } from "./api/authApi";
import { listCirculars, getCircularDetails, processCircularE2E } from "./api/circularsApi";
import { listHitlReviews, approveHitlReview, rejectHitlReview } from "./api/hitlApi";
import { evaluateTransaction, getLedgerEntries, verifyLedgerChain } from "./api/executionApi";

const TOKEN_STORAGE_KEY = "regengine_access_token";

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

function mapCircularDetailsToClauses(details) {
  if (!details?.clauses) return [];
  return details.clauses.map((c) => {
    const activeRule = c.rules?.find((r) => r.is_active) || c.rules?.[0];
    return {
      ruleId: activeRule?.rule_id || `clause-${c.id}`,
      circularNumber: details.circular?.circular_number || "SEBI/2026/01",
      clauseNumber: c.clause_number || "1.0",
      sourceSha256: c.sha256 || "000000000000",
      title: c.section_title || `Clause ${c.clause_number || ""}`,
      rawText: c.text || "",
      highlights: [],
      regoCode: activeRule?.rego_policy || null,
      jsonLogic: activeRule?.jsonlogic_ast || null,
      sampleTransaction: {
        transaction_id: `TXN-${c.clause_number || "001"}`,
        entity_type: "Stockbroker",
        facts: { upfront_margin_pct: 25 },
      },
      status: activeRule?.is_compiled
        ? activeRule.is_active
          ? "compiled"
          : "draft"
        : activeRule?.hitl_status === "BLOCKING"
        ? "hitl_blocked"
        : "draft",
    };
  });
}

function mapHitlReviewsToCases(reviews) {
  if (!reviews) return [];
  return reviews.map((r) => ({
    caseId: r.review_id,
    kind: "compiler",
    ruleId: r.compiled_rule_id ? `rule-${r.compiled_rule_id}` : `clause-${r.clause_id}`,
    clauseNumber: `${r.clause_id}`,
    circularNumber: "SEBI Circular",
    reasonCode: r.reason_code,
    severity: r.severity || "blocking",
    description: r.description,
    sourceExcerpt: r.source_excerpt,
    flaggedAt: r.flagged_at || new Date().toISOString(),
    status: (r.status || "pending").toLowerCase(),
    resolvedBy: r.compliance_officer_id,
    resolvedAt: r.resolved_at,
    notes: r.resolution_notes,
  }));
}

function mapLedgerEntries(entries) {
  if (!entries) return [];
  return entries.map((r) => ({
    sequenceNum: r.sequence_num ?? r.sequenceNum,
    transactionId: r.transaction_id ?? r.transactionId,
    brokerId: r.broker_id ?? r.brokerId ?? "SYSTEM",
    evaluatedAt: r.evaluated_at ?? r.evaluatedAt ?? new Date().toISOString(),
    circularId: r.circular_id ?? r.circularId ?? "SEBI",
    clauseHash: r.clause_hash ?? r.clauseHash ?? "000000000000",
    sectionReference: r.section_reference ?? r.sectionReference ?? "1.0",
    ruleId: r.rule_id ?? r.ruleId,
    evaluationResult: (r.evaluation_result ?? r.evaluationResult ?? "PASS").toUpperCase(),
    hitlReviewId: r.hitl_review_id ?? r.hitlReviewId,
    previousHash: r.previous_hash ?? r.previousHash ?? "genesis",
    currentHash: r.current_hash ?? r.currentHash,
  }));
}

function buildPipelineRuns(circularsList) {
  if (!circularsList) return [];
  return circularsList.map((c) => {
    const isFailed = c.status === "failed" || c.processing_state === "FAILED";
    const isDeployed = c.status === "deployed" || c.processing_state === "DEPLOYED";
    const isApproved = c.status === "approved" || c.processing_state === "APPROVED";
    const isReview = c.status === "review_required" || c.processing_state === "AWAITING_HITL";
    const isExtracting = c.processing_state === "EXTRACTING";
    const isExtracted = c.processing_state === "EXTRACTED";
    const isCompiling = c.processing_state === "COMPILING";

    let currentStage = "compilation";
    if (isFailed) {
      currentStage = "failed";
    } else if (isDeployed || isApproved) {
      currentStage = "done";
    } else if (isReview) {
      currentStage = "verification";
    } else if (isExtracting) {
      currentStage = "extraction";
    } else if (isExtracted || isCompiling) {
      currentStage = "compilation";
    }

    const runStatus = isFailed
      ? "failed"
      : isDeployed
      ? "deployed"
      : isApproved
      ? "approved"
      : isReview
      ? "review_required"
      : "processing";

    return {
      id: `run-${c.id}`,
      filename: c.source_filename || `${c.circular_number || "Circular"}.pdf`,
      sourceUrl: c.source_url || null,
      circularNumber: c.circular_number,
      startedAt: c.created_at || new Date().toISOString(),
      currentStage,
      status: runStatus,
      errorMessage: c.error_message || null,
      stages: {
        ingestion: {
          status: isFailed && (c.clause_count || 0) === 0 ? "failed" : "complete",
          detail: `${c.clause_count || 0} clauses parsed and stored`,
          durationMs: 1200,
        },
        extraction: {
          status: isFailed && (c.clause_count || 0) === 0
            ? "failed"
            : isExtracting
            ? "in_progress"
            : "complete",
          detail: `${c.clause_count || 0} clauses extracted into structured rules`,
          durationMs: 2400,
        },
        verification: {
          status: isFailed
            ? "failed"
            : isReview
            ? "complete"
            : isExtracting
            ? "pending"
            : "complete",
          detail: `${c.pending_reviews || 0} flagged for HITL, ${c.active_rules || 0} active`,
          durationMs: 800,
        },
        compilation: {
          status: isFailed
            ? "failed"
            : (c.active_rules > 0 || isDeployed || isApproved)
            ? "complete"
            : isReview
            ? "pending"
            : "in_progress",
          detail: isFailed
            ? (c.error_message || "Processing failed")
            : `${c.active_rules || 0} Rego policies compiled & deployed`,
          durationMs: 500,
        },
      },
    };
  });
}

export default function App() {
  const [session, setSession] = useState(loadStoredToken);
  const [authLoading, setAuthLoading] = useState(false);
  const [authError, setAuthError] = useState(null);
  const [authModal, setAuthModal] = useState({ open: false, mode: "login" });

  const [activeView, setActiveView] = useState("pipeline");
  const [clauses, setClauses] = useState([]);
  const [hitlCases, setHitlCases] = useState([]);
  const [ledgerFeed, setLedgerFeed] = useState([]);
  const [pipelineRuns, setPipelineRuns] = useState([]);
  const [selectedCircularId, setSelectedCircularId] = useState(null);

  const [dataLoading, setDataLoading] = useState(false);
  const [dataError, setDataError] = useState(null);

  const [uploadState, setUploadState] = useState("idle"); // idle | uploading | processing | success | error
  const [uploadResult, setUploadResult] = useState(null);
  const [uploadError, setUploadError] = useState(null);

  const openAuthModal = (mode) => {
    setAuthError(null);
    setAuthModal({ open: true, mode });
  };
  const closeAuthModal = () => setAuthModal((prev) => ({ ...prev, open: false }));

  const isAuthenticated = Boolean(session);
  const user = session?.claims
    ? { email: session.claims.sub, name: session.claims.sub, roles: session.claims.roles }
    : null;

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

  const loadBackendData = async (targetCircularId = null) => {
    if (!session?.token) return;
    const token = session.token;
    setDataLoading(true);
    setDataError(null);
    try {
      const [circList, hitlList, ledgerList] = await Promise.all([
        listCirculars({ accessToken: token }).catch((err) => {
          console.warn("Failed to fetch circulars:", err);
          return [];
        }),
        listHitlReviews({ accessToken: token }).catch((err) => {
          console.warn("Failed to fetch reviews:", err);
          return [];
        }),
        getLedgerEntries({ limit: 50, accessToken: token }).catch((err) => {
          console.warn("Failed to fetch ledger entries:", err);
          return [];
        }),
      ]);

      setPipelineRuns(buildPipelineRuns(circList));
      setHitlCases(mapHitlReviewsToCases(hitlList));
      setLedgerFeed(mapLedgerEntries(ledgerList));

      const activeId = targetCircularId || selectedCircularId || circList?.[0]?.id;
      if (activeId) {
        setSelectedCircularId(activeId);
        const details = await getCircularDetails(activeId, { accessToken: token }).catch(() => null);
        if (details) {
          setClauses(mapCircularDetailsToClauses(details));
        }
      } else {
        setClauses([]);
      }
    } catch (err) {
      console.error("Failed to load initial backend state:", err);
      setDataError(err instanceof Error ? err.message : "Failed to load backend state.");
    } finally {
      setDataLoading(false);
    }
  };

  useEffect(() => {
    if (isAuthenticated) {
      loadBackendData();
    }
  }, [isAuthenticated, session?.token]);

  useEffect(() => {
    if (!isAuthenticated) return;

    const hasActiveRun = pipelineRuns.some(
      (r) => r.status === "processing" || r.currentStage === "compilation" || r.currentStage === "extraction"
    );
    const pollIntervalMs = hasActiveRun || uploadState === "processing" ? 3000 : 15000;

    const timer = setInterval(() => {
      loadBackendData();
    }, pollIntervalMs);

    return () => clearInterval(timer);
  }, [isAuthenticated, session?.token, pipelineRuns, uploadState]);

  const handleLogin = async (email, password, rememberMe = true) => {
    setAuthLoading(true);
    setAuthError(null);
    try {
      const result = await loginRequest(email, password);
      const store = rememberMe ? localStorage : sessionStorage;
      store.setItem(TOKEN_STORAGE_KEY, result.access_token);
      setSession({ token: result.access_token, claims: decodeToken(result.access_token) });
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setAuthLoading(false);
    }
  };

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

  const handleUpload = async (file) => {
    setUploadState("uploading");
    setUploadError(null);
    setUploadResult(null);
    try {
      if (!session?.token) {
        throw new Error("Not logged in. Log in first.");
      }
      setUploadState("processing");
      const e2eResult = await processCircularE2E(file, { accessToken: session.token });

      setUploadResult({
        filename: file.name,
        chunksIndexed: e2eResult.clause_count,
        status: e2eResult.status,
      });
      setUploadState("success");

      const details = await getCircularDetails(e2eResult.circular_id, { accessToken: session.token });
      if (details) {
        const mappedClauses = mapCircularDetailsToClauses(details);
        setClauses(mappedClauses);
      }

      await loadBackendData(e2eResult.circular_id);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload processing failed.");
      setUploadState("error");
      await loadBackendData();
    }
  };

  const resolveHitlCase = async (caseId, decisionStatus, notes) => {
    if (!session?.token) return;
    try {
      if (decisionStatus === "approved") {
        await approveHitlReview(caseId, { notes, accessToken: session.token });
      } else {
        await rejectHitlReview(caseId, { notes, accessToken: session.token });
      }

      setHitlCases((prev) =>
        prev.map((c) =>
          c.caseId === caseId
            ? {
                ...c,
                status: decisionStatus === "approved" ? "resolved" : "rejected",
                notes: notes || c.notes,
                resolvedBy: user?.email || "compliance_officer",
                resolvedAt: new Date().toISOString(),
              }
            : c
        )
      );

      await loadBackendData();
    } catch (err) {
      alert(`Approval error: ${err instanceof Error ? err.message : err}`);
    }
  };

  const handleBackendEvaluate = async (transactionPayload) => {
    if (!session?.token) throw new Error("Authentication required for live evaluation.");
    const result = await evaluateTransaction(transactionPayload, { accessToken: session.token });
    const freshEntries = await getLedgerEntries({ limit: 50, accessToken: session.token }).catch(() => []);
    setLedgerFeed(mapLedgerEntries(freshEntries));
    return result;
  };

  const submitPlaygroundDraftForReview = async (draft) => {
    setHitlCases((prev) => [
      {
        caseId: `hitl-pg-${Date.now()}`,
        kind: "playground",
        ruleId: draft.ruleId,
        clauseNumber: draft.clauseNumber,
        circularNumber: draft.circularNumber,
        description: draft.lastEvaluation
          ? `Policy Playground submission. Local eval: ${draft.lastEvaluation.allow ? "ALLOW" : "DENY"}.`
          : "Policy Playground submission.",
        editedCode: draft.editedRego || JSON.stringify(draft.editedJsonLogic, null, 2),
        flaggedAt: new Date().toISOString(),
        status: "pending",
      },
      ...prev,
    ]);
  };

  const handleSelectRun = async (run) => {
    const circId = run.id.replace(/^run-/, "");
    setSelectedCircularId(circId);
    if (session?.token) {
      setDataLoading(true);
      try {
        const details = await getCircularDetails(circId, { accessToken: session.token });
        if (details) {
          setClauses(mapCircularDetailsToClauses(details));
        }
      } catch (err) {
        console.error("Failed to load circular details:", err);
      } finally {
        setDataLoading(false);
      }
    }
    setActiveView("splitview");
  };

  const pendingHitlCount = hitlCases.filter((c) => c.status === "pending").length;

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
          {dataError && (
            <div className="mb-4 flex items-center justify-between rounded-sm border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
              <span>Failed to fetch backend data: {dataError}</span>
              <button
                onClick={() => loadBackendData()}
                className="rounded bg-red-100 px-2.5 py-1 text-xs font-semibold text-red-800 hover:bg-red-200"
              >
                Retry
              </button>
            </div>
          )}
          {activeView === "pipeline" && (
            <PipelineTracker
              runs={pipelineRuns}
              onUpload={handleUpload}
              uploadState={uploadState}
              uploadResult={uploadResult}
              uploadError={uploadError}
              isLoading={dataLoading}
              onSelectRun={handleSelectRun}
            />
          )}
          {activeView === "splitview" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <ClauseSplitView clauses={clauses} isLoading={dataLoading} />
            </div>
          )}
          {activeView === "playground" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <PolicyPlayground
                clauses={clauses}
                onSubmitForReview={submitPlaygroundDraftForReview}
                onBackendEvaluate={handleBackendEvaluate}
              />
            </div>
          )}
          {activeView === "hitl" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <HITLDashboard
                cases={hitlCases}
                onResolveCase={resolveHitlCase}
                isLoading={dataLoading}
              />
            </div>
          )}
          {activeView === "vault" && (
            <div className="h-[calc(100vh-7.5rem)]">
              <AuditVault
                initialFeed={ledgerFeed}
                onRefreshFeed={async () => {
                  if (!session?.token) return [];
                  const fresh = await getLedgerEntries({ limit: 50, accessToken: session.token });
                  return mapLedgerEntries(fresh);
                }}
                onVerifyChain={async () => {
                  if (!session?.token) throw new Error("Authentication required.");
                  return await verifyLedgerChain({ accessToken: session.token });
                }}
              />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
