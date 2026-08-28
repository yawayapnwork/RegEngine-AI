import { useState } from "react";
import Sidebar from "./components/layout/Sidebar";
import TopBar from "./components/layout/TopBar";
import PipelineTracker from "./components/pipeline/PipelineTracker";
import ClauseSplitView from "./components/splitview/ClauseSplitView";
import HITLDashboard from "./components/hitl/HITLDashboard";
import AuditVault from "./components/vault/AuditVault";
import {
  clauses,
  hitlCases as initialHitlCases,
  ledgerFeed,
  pipelineRuns,
} from "./mock/mockData";

export default function App() {
  const [activeView, setActiveView] = useState("pipeline");
  const [hitlCases, setHitlCases] = useState(initialHitlCases);

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
        <TopBar activeView={activeView} />
        <main className="flex-1 overflow-y-auto scrollbar-thin p-6">
          {activeView === "pipeline" && (
            <PipelineTracker
              runs={pipelineRuns}
              onUpload={(file) => console.log("Uploaded (mock):", file.name)}
            />
          )}
          {activeView === "splitview" && (
            <div className="h-[calc(100vh-9.5rem)]">
              <ClauseSplitView clauses={clauses} />
            </div>
          )}
          {activeView === "hitl" && (
            <div className="h-[calc(100vh-9.5rem)]">
              <HITLDashboard
                cases={hitlCases}
                onResolveCase={resolveHitlCase}
              />
            </div>
          )}
          {activeView === "vault" && (
            <div className="h-[calc(100vh-9.5rem)]">
              <AuditVault initialFeed={ledgerFeed} />
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
