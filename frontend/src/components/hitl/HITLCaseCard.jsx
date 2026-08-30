import { Check, Clock, FileCode2, FlaskConical, Gavel, X } from "lucide-react";
import { useState } from "react";
import StatusBadge from "../shared/StatusBadge";

const ICONS = {
  compiler: { Icon: FileCode2, classes: "bg-violet-100 text-violet-700" },
  playground: { Icon: FlaskConical, classes: "bg-blue-100 text-blue-700" },
  execution: { Icon: Gavel, classes: "bg-amber-100 text-amber-700" },
};

export default function HITLCaseCard({ hitlCase, onResolve }) {
  const [notes, setNotes] = useState("");
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

  return (
    <div className="grid grid-cols-[1fr_1.6fr_1.4fr_auto] gap-4 px-4 py-3 text-sm hover:bg-ink-850">
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
  );
}
