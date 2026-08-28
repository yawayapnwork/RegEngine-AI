import { Check, Clock, FileCode2, Gavel, X } from "lucide-react";
import { useState } from "react";
import Card from "../shared/Card";
import StatusBadge from "../shared/StatusBadge";

export default function HITLCaseCard({ hitlCase, onResolve }) {
  const [notes, setNotes] = useState("");
  const isPending = hitlCase.status === "pending";
  const isCompilerCase = hitlCase.kind === "compiler";

  return (
    <Card className="p-5">
      <div className="mb-3 flex items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <div
            className={`mt-0.5 rounded-lg p-2 ${isCompilerCase ? "bg-violet-500/10" : "bg-amber-500/10"}`}
          >
            {isCompilerCase ? (
              <FileCode2 className="h-4 w-4 text-violet-400" />
            ) : (
              <Gavel className="h-4 w-4 text-amber-400" />
            )}
          </div>
          <div>
            <p className="text-sm font-semibold text-slate-100">
              {isCompilerCase
                ? `Clause ${hitlCase.clauseNumber} &mdash; compile-time flag`
                : `Transaction ${hitlCase.transactionId}`}
            </p>
            <p className="text-xs text-slate-500">
              {isCompilerCase
                ? hitlCase.circularNumber
                : `Broker ${hitlCase.brokerId}`}{" "}
              &middot; rule {hitlCase.ruleId}
            </p>
          </div>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <StatusBadge
            status={isCompilerCase ? hitlCase.severity : "hitl_review"}
          />
          <span className="flex items-center gap-1 text-[11px] text-slate-600">
            <Clock className="h-3 w-3" />{" "}
            {new Date(hitlCase.flaggedAt).toLocaleString()}
          </span>
        </div>
      </div>

      <p className="mb-2 rounded-lg bg-ink-850 px-3 py-2 text-sm text-slate-300">
        {isCompilerCase ? hitlCase.description : hitlCase.reason}
      </p>

      {isCompilerCase && hitlCase.sourceExcerpt && (
        <p className="mb-3 border-l-2 border-ink-600 pl-3 text-xs italic text-slate-500">
          &ldquo;{hitlCase.sourceExcerpt}&rdquo;
        </p>
      )}
      {!isCompilerCase && (
        <pre className="mb-3 overflow-x-auto rounded-lg bg-ink-950 px-3 py-2 text-xs text-slate-400">
          {JSON.stringify(hitlCase.facts, null, 2)}
        </pre>
      )}

      {isPending ? (
        <div className="flex items-center gap-2">
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Resolution notes (recorded on the case + ledger event)..."
            className="flex-1 rounded-lg border border-ink-700 bg-ink-850 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:border-sky-500 focus:outline-none"
          />
          <button
            onClick={() => onResolve(hitlCase.caseId, "approved", notes)}
            className="flex items-center gap-1.5 rounded-lg bg-emerald-500/15 px-3 py-2 text-sm font-medium text-emerald-400 ring-1 ring-inset ring-emerald-500/30 hover:bg-emerald-500/25"
          >
            <Check className="h-4 w-4" /> Approve
          </button>
          <button
            onClick={() => onResolve(hitlCase.caseId, "rejected", notes)}
            className="flex items-center gap-1.5 rounded-lg bg-rose-500/15 px-3 py-2 text-sm font-medium text-rose-400 ring-1 ring-inset ring-rose-500/30 hover:bg-rose-500/25"
          >
            <X className="h-4 w-4" /> Reject
          </button>
        </div>
      ) : (
        <div className="rounded-lg bg-ink-850 px-3 py-2 text-xs text-slate-500">
          Resolved{" "}
          <span className="font-medium text-slate-400">{hitlCase.status}</span>{" "}
          by {hitlCase.resolvedBy} &middot;{" "}
          {new Date(hitlCase.resolvedAt).toLocaleString()}
          {hitlCase.notes && (
            <p className="mt-1 text-slate-400">
              &ldquo;{hitlCase.notes}&rdquo;
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
