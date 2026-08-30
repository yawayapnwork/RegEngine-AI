import { PIPELINE_STAGES } from "../../mock/mockData";

const STAGE_LABELS = {
  ingestion: "Ingestion",
  extraction: "Extraction",
  verification: "Verification",
  compilation: "Compilation",
};

function segmentClasses(status) {
  if (status === "complete") return "bg-green-500";
  if (status === "in_progress") return "bg-blue-500";
  return "bg-ink-700";
}

/** Compact linear status tracker: four thin segments, one per pipeline
 * stage, in place of the earlier oversized circular step nodes. Hover a
 * segment for that stage's detail; the metrics table columns carry the
 * numbers that matter, so this only needs to show progress at a glance. */
export default function PipelineStage({ stages }) {
  return (
    <div className="flex min-w-[9rem] flex-col gap-1">
      <div className="flex items-center gap-0.5">
        {PIPELINE_STAGES.map((stageId) => (
          <div
            key={stageId}
            title={`${STAGE_LABELS[stageId]}: ${stages[stageId].detail}`}
            className={`h-1.5 flex-1 ${segmentClasses(stages[stageId].status)} ${
              stages[stageId].status === "in_progress" ? "animate-pulse" : ""
            }`}
          />
        ))}
      </div>
      <div className="flex justify-between font-mono text-[10px] uppercase tracking-wide text-slate-400">
        {PIPELINE_STAGES.map((stageId) => (
          <span key={stageId}>{STAGE_LABELS[stageId].slice(0, 3)}</span>
        ))}
      </div>
    </div>
  );
}
