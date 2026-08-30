import { Check, Clock, FileCode2, FlaskConical, Gavel, X } from "lucide-react";
import { useState } from "react";
import StatusBadge from "../shared/StatusBadge";

const ICONS = {
  compiler: { Icon: FileCode2, classes: "bg-violet-500/10 text-violet-400" },
  playground: { Icon: FlaskConical, classes: "bg-sky-500/10 text-sky-400" },
  execution: { Icon: Gavel, classes: "bg-amber-500/10 text-amber-400" },
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
    <div className="grid grid-cols-[1fr_1.6fr_1.4fr_auto] gap-4 px-4 py-3 text-sm">
      <div className="flex items-start gap-2.5">
        <div className={`mt-0.5 rounded-sm p-1.5 ${classes}`}>
          <Icon className="h-3.5 w-3.5" />
        </div>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-slate-100">{title}</p>
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
            <span className="flex items-center gap-1 text-2xs text-slate-600">
              <Clock className="h-3 w-3" />
              {new Date(hitlCase.flaggedAt).toLocaleString()}
            </span>
          </div>
        </div>
      </div>

      <div className="min-w-0">
        <p className="rounded-sm bg-ink-850 px-2.5 py-2 text-xs leading-relaxed text-slate-300">
          {isCompilerCase && hitlCase.description}
          {isPlaygroundCase && hitlCase.description}
          {!isCompilerCase && !isPlaygroundCase && hitlCase.reason}
        </p>

        {isCompilerCase && hitlCase.sourceExcerpt && (
          <p className="mt-2 border-l-2 border-ink-600 pl-2 text-xs italic text-slate-500">
            &ldquo;{hitlCase.sourceExcerpt}&rdquo;
          </p>
        )}
        {isPlaygroundCase && hitlCase.editedCode && (
          <pre className="mt-2 max-h-28 overflow-auto rounded-sm bg-ink-950 px-2.5 py-2 font-mono text-2xs text-slate-400">
            {hitlCase.editedCode}
          </pre>
        )}
        {!isCompilerCase && !isPlaygroundCase && (
          <pre className="mt-2 max-h-28 overflow-auto rounded-sm bg-ink-950 px-2.5 py-2 font-mono text-2xs text-slate-400">
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
          className="w-full resize-none rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-2 text-xs text-slate-200 placeholder:text-slate-600 focus:border-sky-500 focus:outline-none"
        />
      ) : (
        <div className="rounded-sm bg-ink-850 px-2.5 py-2 text-2xs text-slate-500">
          Resolved{" "}
          <span className="font-medium text-slate-400">{hitlCase.status}</span>{" "}
          by {hitlCase.resolvedBy} &middot;{" "}
          {new Date(hitlCase.resolvedAt).toLocaleString()}
          {hitlCase.notes && (
            <p className="mt-1 text-slate-400">&ldquo;{hitlCase.notes}&rdquo;</p>
          )}
        </div>
      )}

      {isPending ? (
        <div className="flex flex-col gap-1.5">
          <button
            onClick={() => onResolve(hitlCase.caseId, "approved", notes)}
            disabled={!canResolve}
            title={canResolve ? undefined : "An audit note is required before resolving this case"}
            className="flex items-center justify-center gap-1.5 rounded-sm border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs font-semibold text-emerald-400 hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Check className="h-3.5 w-3.5" /> Approve
          </button>
          <button
            onClick={() => onResolve(hitlCase.caseId, "rejected", notes)}
            disabled={!canResolve}
            title={canResolve ? undefined : "An audit note is required before resolving this case"}
            className="flex items-center justify-center gap-1.5 rounded-sm border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs font-semibold text-rose-400 hover:bg-rose-500/20 disabled:cursor-not-allowed disabled:opacity-40"
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
