import { AlertTriangle, CheckCircle2, FileText, Loader2 } from "lucide-react";
import Card from "../shared/Card";
import StatusBadge from "../shared/StatusBadge";
import PdfUploadZone from "./PdfUploadZone";
import PipelineStage from "./PipelineStage";

const UPLOADING_LABELS = {
  uploading: "Uploading...",
  queued: "Queued — waiting for a worker to pick this up...",
  processing: "Processing — running OCR/extraction and indexing (large circulars can take a while)...",
};

function UploadStatusBanner({ uploadState, uploadResult, uploadError }) {
  if (uploadState === "idle") return null;

  if (uploadState in UPLOADING_LABELS) {
    return (
      <div className="flex items-center gap-2 rounded-sm border border-ink-700 bg-ink-850 px-3 py-2 text-sm text-slate-600">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> {UPLOADING_LABELS[uploadState]}
      </div>
    );
  }
  if (uploadState === "error") {
    return (
      <div className="flex items-center gap-2 rounded-sm border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> Upload failed: {uploadError}
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 rounded-sm border border-green-200 bg-green-100 px-3 py-2 text-sm text-green-800">
      <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
      Indexed <span className="font-mono">{uploadResult?.filename}</span> — {uploadResult?.chunksIndexed} clause chunk(s) indexed.
    </div>
  );
}

function extractNumber(detail, pattern) {
  const match = detail?.match(pattern);
  return match ? match[1] : "—";
}

function metricsFor(run) {
  return {
    extractionMs: run.stages.extraction.durationMs,
    layoutElements: extractNumber(
      run.stages.ingestion.detail,
      /(\d+)\s+layout elements/,
    ),
    rulesExtracted: extractNumber(
      run.stages.extraction.detail,
      /(\d+)\s+extracted rules/,
    ),
  };
}

function RunRow({ run, onSelectRun }) {
  const isFailed = run.status === "failed" || run.currentStage === "failed";
  const overallDone = run.currentStage === "done";
  const isReview = run.status === "review_required" || run.currentStage === "verification";
  const m = metricsFor(run);

  const badgeStatus = isFailed
    ? "failed"
    : overallDone
    ? "complete"
    : isReview
    ? "hitl_review"
    : "in_progress";

  const badgeLabel = isFailed
    ? "failed"
    : overallDone
    ? "complete"
    : isReview
    ? "review req"
    : "running";

  return (
    <tr
      onClick={() => onSelectRun?.(run)}
      className={`border-b border-ink-700 text-sm last:border-b-0 even:bg-ink-850 ${
        onSelectRun ? "cursor-pointer hover:bg-ink-800/50" : ""
      }`}
    >
      <td className="px-4 py-3">
        <div className="flex items-start gap-2">
          <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
          <div>
            <p className="font-medium leading-tight text-slate-800">
              {run.filename}
            </p>
            <p className="mt-0.5 font-mono text-2xs text-slate-500">
              {run.circularNumber}
            </p>
          </div>
        </div>
      </td>
      <td className="whitespace-nowrap px-4 py-3 font-mono text-2xs text-slate-500">
        {new Date(run.startedAt).toLocaleString()}
      </td>
      <td className="px-4 py-3">
        <PipelineStage stages={run.stages} />
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs tabular-nums text-slate-600">
        {m.extractionMs != null ? `${(m.extractionMs / 1000).toFixed(1)}s` : "—"}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs tabular-nums text-slate-600">
        {m.layoutElements}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-xs tabular-nums text-slate-600">
        {m.rulesExtracted}
      </td>
      <td className="whitespace-nowrap px-4 py-3">
        <StatusBadge
          status={badgeStatus}
          label={badgeLabel}
        />
      </td>
    </tr>
  );
}

export default function PipelineTracker({
  runs = [],
  onUpload,
  uploadState = "idle",
  uploadResult = null,
  uploadError = null,
  isLoading = false,
  onSelectRun,
}) {
  return (
    <div className="flex flex-col gap-4">
      <PdfUploadZone onFileSelected={onUpload} disabled={uploadState in UPLOADING_LABELS} />
      <UploadStatusBanner uploadState={uploadState} uploadResult={uploadResult} uploadError={uploadError} />

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] border-collapse">
            <thead className="border-b border-ink-700 bg-ink-850 text-left text-2xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2.5 font-semibold">Document</th>
                <th className="px-4 py-2.5 font-semibold">Started</th>
                <th className="px-4 py-2.5 font-semibold">Pipeline</th>
                <th className="px-4 py-2.5 text-right font-semibold">
                  Extraction Time
                </th>
                <th className="px-4 py-2.5 text-right font-semibold">
                  Layout Elements
                </th>
                <th className="px-4 py-2.5 text-right font-semibold">
                  Rules Extracted
                </th>
                <th className="px-4 py-2.5 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr>
                  <td colSpan={7} className="py-12 text-center text-sm text-slate-500">
                    <div className="flex items-center justify-center gap-2">
                      <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
                      <span>Loading circular pipeline runs...</span>
                    </div>
                  </td>
                </tr>
              ) : runs.length === 0 ? (
                <tr>
                  <td colSpan={7} className="py-12 text-center text-sm text-slate-500">
                    <FileText className="mx-auto mb-2 h-7 w-7 text-slate-400" />
                    <p className="font-medium text-slate-700">No circular pipeline runs yet</p>
                    <p className="mt-0.5 text-xs text-slate-400">
                      Upload a regulatory circular PDF above to begin automated ingestion, extraction, and rule compilation.
                    </p>
                  </td>
                </tr>
              ) : (
                runs.map((run) => (
                  <RunRow key={run.id} run={run} onSelectRun={onSelectRun} />
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
