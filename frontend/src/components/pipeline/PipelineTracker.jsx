import { FileText } from "lucide-react";
import Card from "../shared/Card";
import StatusBadge from "../shared/StatusBadge";
import PdfUploadZone from "./PdfUploadZone";
import PipelineStage from "./PipelineStage";

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

function RunRow({ run }) {
  const overallDone = run.currentStage === "done";
  const m = metricsFor(run);

  return (
    <tr className="border-b border-ink-700 text-sm last:border-b-0 even:bg-ink-850">
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
          status={overallDone ? "complete" : "in_progress"}
          label={overallDone ? "complete" : "running"}
        />
      </td>
    </tr>
  );
}

export default function PipelineTracker({ runs, onUpload }) {
  return (
    <div className="flex flex-col gap-4">
      <PdfUploadZone onFileSelected={onUpload} />

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
              {runs.map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
