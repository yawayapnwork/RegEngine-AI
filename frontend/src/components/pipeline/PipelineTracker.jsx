import { FileText } from "lucide-react";
import { PIPELINE_STAGES } from "../../mock/mockData";
import Card from "../shared/Card";
import StatusBadge from "../shared/StatusBadge";
import PdfUploadZone from "./PdfUploadZone";
import PipelineStage from "./PipelineStage";

function connectorClass(fromStatus) {
  if (fromStatus === "complete") return "bg-emerald-500";
  if (fromStatus === "in_progress")
    return "bg-gradient-to-r from-sky-500 to-ink-700";
  return "bg-ink-700";
}

function RunCard({ run }) {
  const overallDone = run.currentStage === "done";
  return (
    <Card className="p-6">
      <div className="mb-6 flex items-start justify-between">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 rounded-lg bg-ink-800 p-2">
            <FileText className="h-5 w-5 text-sky-400" />
          </div>
          <div>
            <p className="text-sm font-semibold text-slate-100">
              {run.filename}
            </p>
            <p className="text-xs text-slate-500">
              {run.circularNumber} &middot; started{" "}
              {new Date(run.startedAt).toLocaleString()}
            </p>
          </div>
        </div>
        <StatusBadge
          status={overallDone ? "complete" : "in_progress"}
          label={overallDone ? "pipeline complete" : "running"}
        />
      </div>

      <div className="flex items-start">
        {PIPELINE_STAGES.map((stageId, i) => (
          <div key={stageId} className="flex flex-1 items-start">
            <PipelineStage stageId={stageId} stage={run.stages[stageId]} />
            {i < PIPELINE_STAGES.length - 1 && (
              <div
                className={`mt-5 h-0.5 flex-1 rounded ${connectorClass(run.stages[stageId].status)}`}
              />
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function PipelineTracker({ runs, onUpload }) {
  return (
    <div className="flex flex-col gap-6">
      <PdfUploadZone onFileSelected={onUpload} />
      <div className="flex flex-col gap-4">
        {runs.map((run) => (
          <RunCard key={run.id} run={run} />
        ))}
      </div>
    </div>
  );
}
