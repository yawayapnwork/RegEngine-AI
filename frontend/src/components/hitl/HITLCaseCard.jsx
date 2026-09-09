import {
  Check,
  ChevronDown,
  ChevronUp,
  Clock,
  FileCode2,
  FlaskConical,
  Gavel,
  Loader2,
  Shield,
  ShieldAlert,
  X,
} from "lucide-react";
import { useState } from "react";
import StatusBadge from "../shared/StatusBadge";
import { getHitlReviewEvidence } from "../../api/hitlApi";

const ICONS = {
  compiler: { Icon: FileCode2, classes: "bg-violet-100 text-violet-700" },
  playground: { Icon: FlaskConical, classes: "bg-blue-100 text-blue-700" },
  execution: { Icon: Gavel, classes: "bg-amber-100 text-amber-700" },
};

export default function HITLCaseCard({ hitlCase, onResolve }) {
  const [notes, setNotes] = useState("");
  const [isEvidenceOpen, setIsEvidenceOpen] = useState(false);
  const [evidence, setEvidence] = useState(null);
  const [isEvidenceLoading, setIsEvidenceLoading] = useState(false);
  const [evidenceError, setEvidenceError] = useState(null);

  const isPending = hitlCase.status === "pending";
  const isCompilerCase = hitlCase.kind === "compiler";
  const isPlaygroundCase = hitlCase.kind === "playground";
  const { Icon, classes } = ICONS[hitlCase.kind] || ICONS.execution;
  const canResolve = notes.trim().length > 0;

  const title = isCompilerCase
    ? `Clause ${hitlCase.clauseNumber} — compile-time flag`
    : isPlaygroundCase
      ? `Clause ${hitlCase.clauseNumber} — playground submission`
      : `Transaction ${hitlCase.transactionId}`;

  const toggleEvidence = async () => {
    const nextState = !isEvidenceOpen;
    setIsEvidenceOpen(nextState);
    if (nextState && !evidence && !isEvidenceLoading && isCompilerCase) {
      setIsEvidenceLoading(true);
      setEvidenceError(null);
      try {
        const data = await getHitlReviewEvidence(hitlCase.caseId);
        setEvidence(data);
      } catch (err) {
        setEvidenceError(err?.message || "Failed to fetch review evidence.");
      } finally {
        setIsEvidenceLoading(false);
      }
    }
  };

  return (
    <div className="flex flex-col border-b border-ink-800 hover:bg-ink-850/50">
      <div className="grid grid-cols-[1fr_1.6fr_1.4fr_auto] gap-4 px-4 py-3 text-sm">
        <div className="flex items-start gap-2.5">
          <div className={`mt-0.5 rounded-sm p-1.5 ${classes}`}>
            <Icon className="h-3.5 w-3.5" />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-slate-900">{title}</p>
            <p className="text-2xs text-slate-500">
              {isCompilerCase || isPlaygroundCase
                ? hitlCase.circularNumber
                : `Broker ${hitlCase.brokerId}`}{" "}
              &middot; rule {hitlCase.ruleId}
            </p>
            <div className="mt-1.5 flex items-center gap-2">
              <StatusBadge
                status={isCompilerCase ? hitlCase.severity : isPlaygroundCase ? "advisory" : "hitl_review"}
              />
              <span className="flex items-center gap-1 text-2xs text-slate-400">
                <Clock className="h-3 w-3" />
                {new Date(hitlCase.flaggedAt).toLocaleString()}
              </span>
            </div>
            {isCompilerCase && (
              <button
                type="button"
                onClick={toggleEvidence}
                className="mt-2 flex items-center gap-1 text-2xs font-medium text-blue-600 hover:text-blue-800"
              >
                {isEvidenceOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                {isEvidenceOpen ? "Hide Evidence Dossier" : "View Evidence Dossier"}
              </button>
            )}
          </div>
        </div>

        <div className="min-w-0">
          <p className="rounded-sm bg-ink-850 px-2.5 py-2 text-xs leading-relaxed text-slate-700">
            {isCompilerCase && hitlCase.description}
            {isPlaygroundCase && hitlCase.description}
            {!isCompilerCase && !isPlaygroundCase && hitlCase.reason}
          </p>

          {isCompilerCase && hitlCase.sourceExcerpt && (
            <p className="mt-2 border-l-2 border-ink-650 pl-2 text-xs italic text-slate-500">
              &ldquo;{hitlCase.sourceExcerpt}&rdquo;
            </p>
          )}
          {isPlaygroundCase && hitlCase.editedCode && (
            <pre className="mt-2 max-h-28 overflow-auto rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-2 font-mono text-2xs text-slate-600">
              {hitlCase.editedCode}
            </pre>
          )}
          {!isCompilerCase && !isPlaygroundCase && (
            <pre className="mt-2 max-h-28 overflow-auto rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-2 font-mono text-2xs text-slate-600">
              {JSON.stringify(hitlCase.facts, null, 2)}
            </pre>
          )}
        </div>

        {isPending ? (
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Audit note (required to approve or reject)..."
            rows={3}
            className="w-full resize-none rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-2 text-xs text-slate-800 placeholder:text-slate-400 focus:border-blue-400 focus:outline-none"
          />
        ) : (
          <div className="rounded-sm bg-ink-850 px-2.5 py-2 text-2xs text-slate-500">
            Resolved{" "}
            <span className="font-medium text-slate-600">{hitlCase.status}</span>{" "}
            by {hitlCase.resolvedBy} &middot;{" "}
            {new Date(hitlCase.resolvedAt).toLocaleString()}
            {hitlCase.notes && (
              <p className="mt-1 text-slate-600">&ldquo;{hitlCase.notes}&rdquo;</p>
            )}
          </div>
        )}

        {isPending ? (
          <div className="flex flex-col gap-1.5">
            <button
              onClick={() => onResolve(hitlCase.caseId, "approved", notes)}
              disabled={!canResolve}
              title={canResolve ? undefined : "An audit note is required before resolving this case"}
              className="flex items-center justify-center gap-1.5 rounded-sm border border-green-200 bg-green-100 px-3 py-1.5 text-xs font-semibold text-green-800 hover:bg-green-200 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Check className="h-3.5 w-3.5" /> Approve
            </button>
            <button
              onClick={() => onResolve(hitlCase.caseId, "rejected", notes)}
              disabled={!canResolve}
              title={canResolve ? undefined : "An audit note is required before resolving this case"}
              className="flex items-center justify-center gap-1.5 rounded-sm border border-red-200 bg-red-100 px-3 py-1.5 text-xs font-semibold text-red-800 hover:bg-red-200 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <X className="h-3.5 w-3.5" /> Reject
            </button>
          </div>
        ) : (
          <div />
        )}
      </div>

      {/* Expandable Unified Evidence Package */}
      {isEvidenceOpen && (
        <div className="border-t border-ink-750 bg-ink-900/60 p-4">
          <div className="mb-3 flex items-center justify-between border-b border-ink-750 pb-2">
            <div className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-blue-600" />
              <h4 className="text-xs font-bold uppercase tracking-wide text-slate-800">
                Unified Compliance Evidence Dossier
              </h4>
            </div>
            <span className="text-2xs text-slate-400">
              Sole Approval Authority: <strong className="text-slate-600">Human Compliance Officer</strong>
            </span>
          </div>

          {isEvidenceLoading && (
            <div className="flex items-center gap-2 py-4 text-xs text-slate-500">
              <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
              Loading multi-source evidence artifacts...
            </div>
          )}

          {evidenceError && (
            <div className="rounded border border-red-200 bg-red-50 p-2.5 text-xs text-red-700">
              {evidenceError}
            </div>
          )}

          {evidence && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              {/* 1. Source Evidence */}
              <div className="rounded border border-slate-200 bg-white p-3 shadow-2xs">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="rounded bg-indigo-50 px-2 py-0.5 text-2xs font-semibold text-indigo-700">
                    Source Evidence (Authoritative)
                  </span>
                  <span className="text-3xs font-mono text-slate-400">
                    SHA: {evidence.source_evidence?.source_sha256?.slice(0, 12)}...
                  </span>
                </div>
                <p className="text-xs font-medium text-slate-800">
                  {evidence.source_evidence?.circular_number} &middot; Clause {evidence.source_evidence?.clause_number}
                </p>
                <p className="mt-1 line-clamp-3 text-2xs italic text-slate-600">
                  &ldquo;{evidence.source_evidence?.raw_text}&rdquo;
                </p>
              </div>

              {/* 2. Deterministic Evidence */}
              <div className="rounded border border-slate-200 bg-white p-3 shadow-2xs">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="rounded bg-violet-50 px-2 py-0.5 text-2xs font-semibold text-violet-700">
                    Deterministic Evidence (Authoritative)
                  </span>
                  <span className="text-3xs font-mono text-slate-400">
                    v{evidence.deterministic_evidence?.rule_version} &middot; {evidence.deterministic_evidence?.policy_sha256?.slice(0, 12)}...
                  </span>
                </div>
                <p className="text-xs font-medium text-slate-800">
                  Rule ID: {evidence.deterministic_evidence?.rule_id}
                </p>
                <pre className="mt-1 max-h-16 overflow-auto rounded bg-slate-50 p-1.5 font-mono text-3xs text-slate-700">
                  {JSON.stringify(evidence.deterministic_evidence?.jsonlogic_ast, null, 2)}
                </pre>
              </div>

              {/* 3. AI-Generated Analysis (Arbitration) */}
              <div className="rounded border border-amber-200 bg-amber-50/40 p-3 shadow-2xs">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="rounded bg-amber-100 px-2 py-0.5 text-2xs font-semibold text-amber-800">
                    AI-Generated Analysis (Advisory Only)
                  </span>
                  <span className="text-3xs text-amber-700">
                    {evidence.arbitration_analysis?.status}
                  </span>
                </div>
                <p className="text-2xs text-amber-900/80">
                  {evidence.arbitration_analysis?.disclaimer}
                </p>
                {evidence.arbitration_analysis?.status === "AVAILABLE" && (
                  <div className="mt-2 text-2xs text-slate-700">
                    <p><strong>Outcome:</strong> {evidence.arbitration_analysis?.final_outcome}</p>
                    <p><strong>Confidence:</strong> {((evidence.arbitration_analysis?.arbiter_confidence || 0) * 100).toFixed(1)}%</p>
                    {evidence.arbitration_analysis?.same_model_risk && (
                      <p className="text-amber-800 font-semibold">⚠️ Same-model evaluation risk detected</p>
                    )}
                  </div>
                )}
              </div>

              {/* 4. Historical Precedent */}
              <div className="rounded border border-cyan-200 bg-cyan-50/40 p-3 shadow-2xs">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="rounded bg-cyan-100 px-2 py-0.5 text-2xs font-semibold text-cyan-800">
                    Historical Precedent (Advisory Only)
                  </span>
                  <span className="text-3xs text-cyan-700">
                    {evidence.precedent_evidence?.status}
                  </span>
                </div>
                <p className="text-2xs text-cyan-900/80">
                  {evidence.precedent_evidence?.disclaimer}
                </p>
                {evidence.precedent_evidence?.status === "AVAILABLE" && (
                  <div className="mt-2 text-2xs text-slate-700">
                    <p>Found <strong>{evidence.precedent_evidence?.precedents_count}</strong> similar resolved decisions.</p>
                  </div>
                )}
              </div>

              {/* 5. Simulation (Digital Twin Preview) */}
              <div className="rounded border border-purple-200 bg-purple-50/40 p-3 shadow-2xs">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="rounded bg-purple-100 px-2 py-0.5 text-2xs font-semibold text-purple-800">
                    Simulation (Advisory Only)
                  </span>
                  <span className="text-3xs text-purple-700">
                    {evidence.digital_twin_simulation?.status}
                  </span>
                </div>
                <p className="text-2xs text-purple-900/80">
                  {evidence.digital_twin_simulation?.disclaimer}
                </p>
                {evidence.digital_twin_simulation?.status === "AVAILABLE" && (
                  <div className="mt-2 text-2xs text-slate-700">
                    <p>Replayed <strong>{evidence.digital_twin_simulation?.transactions_evaluated}</strong> past transactions.</p>
                    <p>Newly Failing: <strong className="text-rose-700">{evidence.digital_twin_simulation?.newly_failing_count}</strong></p>
                  </div>
                )}
              </div>

              {/* 6. Cryptographic Proof */}
              <div className="rounded border border-emerald-200 bg-emerald-50/40 p-3 shadow-2xs">
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="rounded bg-emerald-100 px-2 py-0.5 text-2xs font-semibold text-emerald-800">
                    Cryptographic Proof (ZKP)
                  </span>
                  <span className="text-3xs text-emerald-700">
                    {evidence.zkp_evidence?.status}
                  </span>
                </div>
                <p className="text-2xs text-emerald-900/80">
                  {evidence.zkp_evidence?.disclaimer}
                </p>
                {evidence.zkp_evidence?.status === "AVAILABLE" && (
                  <div className="mt-2 text-2xs text-slate-700">
                    <p>Verified proofs logged: <strong>{evidence.zkp_evidence?.proof_count}</strong></p>
                  </div>
                )}
              </div>

              {/* 7. M&A Findings */}
              {evidence.mna_findings?.status === "AVAILABLE" && (
                <div className="rounded border border-rose-200 bg-rose-50/40 p-3 shadow-2xs md:col-span-2">
                  <div className="mb-1.5 flex items-center justify-between">
                    <span className="rounded bg-rose-100 px-2 py-0.5 text-2xs font-semibold text-rose-800">
                      M&A Due-Diligence Cross-Entity Conflicts (Advisory)
                    </span>
                    <span className="text-3xs text-rose-700">
                      {evidence.mna_findings?.findings_count} findings
                    </span>
                  </div>
                  <p className="text-2xs text-rose-900/80">
                    {evidence.mna_findings?.disclaimer}
                  </p>
                  <div className="mt-2 space-y-1">
                    {evidence.mna_findings?.findings.map((f, i) => (
                      <div key={i} className="rounded bg-white p-2 text-2xs border border-rose-100">
                        <p className="font-semibold text-slate-800">{f.title} ({f.difference_type})</p>
                        <p className="text-slate-600">{f.description}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
