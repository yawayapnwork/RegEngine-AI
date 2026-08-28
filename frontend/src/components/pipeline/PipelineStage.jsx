import { Check, Loader2 } from "lucide-react";

const STAGE_LABELS = {
  ingestion: "Ingestion",
  extraction: "Extraction",
  verification: "Verification",
  compilation: "Compilation",
};

function nodeClasses(status) {
  if (status === "complete")
    return "border-emerald-500 bg-emerald-500/20 text-emerald-400";
  if (status === "in_progress")
    return "border-sky-500 bg-sky-500/20 text-sky-400";
  return "border-ink-600 bg-ink-800 text-slate-500";
}

/** Renders just the node + label/detail column for one stage. Connector
 * lines between stages are rendered by the parent (PipelineTracker) so
 * this component never has to know about its neighbors. */
export default function PipelineStage({ stageId, stage }) {
  return (
    <div className="flex w-32 flex-col items-center text-center">
      <div
        className={`flex h-11 w-11 items-center justify-center rounded-full border-2 ${nodeClasses(stage.status)}`}
      >
        {stage.status === "complete" && <Check className="h-5 w-5" />}
        {stage.status === "in_progress" && (
          <Loader2 className="h-5 w-5 animate-spin" />
        )}
        {stage.status === "pending" && (
          <span className="text-sm font-semibold">&bull;</span>
        )}
      </div>
      <p className="mt-2 text-sm font-medium text-slate-200">
        {STAGE_LABELS[stageId]}
      </p>
      <p className="mt-0.5 text-xs text-slate-500">{stage.detail}</p>
      {stage.durationMs != null && (
        <p className="mt-1 text-[11px] text-slate-600">
          {(stage.durationMs / 1000).toFixed(1)}s
        </p>
      )}
    </div>
  );
}
