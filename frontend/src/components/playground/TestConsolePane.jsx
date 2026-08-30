import { AlertTriangle, CheckCircle2, Loader2, Send, ShieldQuestion, XCircle } from "lucide-react";
import Card from "../shared/Card";
import CodeEditor from "./CodeEditor";

function ResultBanner({ result }) {
  if (!result) {
    return (
      <div className="flex items-center gap-2 rounded-sm border border-ink-700 bg-ink-850 px-3 py-2.5 text-sm text-slate-500">
        <ShieldQuestion className="h-4 w-4" /> Edit the payload or policy to see a live result.
      </div>
    );
  }
  if (result.error) {
    return (
      <div className="flex items-start gap-2 rounded-sm border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm text-amber-800">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <div>
          <p className="font-medium">Evaluation error</p>
          <p className="text-xs text-amber-700">{result.error}</p>
        </div>
      </div>
    );
  }
  const allow = Boolean(result.allow);
  return (
    <div
      className={`flex items-center gap-2 rounded-sm border-l-2 border px-3 py-2.5 text-sm font-semibold uppercase tracking-wide ${
        allow ? "border-green-500 border-y-green-200 border-r-green-200 bg-green-50 text-green-800" : "border-red-500 border-y-red-200 border-r-red-200 bg-red-50 text-red-800"
      }`}
    >
      {allow ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
      {allow ? "ALLOW" : "DENY"}
      <span className="ml-auto rounded-sm bg-black/5 px-2 py-0.5 text-2xs font-medium normal-case tracking-normal text-slate-600">
        engine: {result.engine === "opa-wasm" ? "OPA Wasm" : "JSON-Logic (in-browser)"}
      </span>
    </div>
  );
}

/** Right panel (Requirement 1 + 2): the test transaction payload editor
 * and the real-time evaluation result, plus Requirement 3's "Submit for
 * HITL Review" workflow. */
export default function TestConsolePane({
  payloadText,
  onPayloadTextChange,
  payloadParseError,
  result,
  submitState,
  submitError,
  onSubmitForReview,
  canSubmit,
}) {
  return (
    <Card className="flex flex-col overflow-hidden">
      <div className="border-b border-ink-700 px-4 py-3">
        <span className="text-sm font-medium text-slate-700">Test Transaction Payload</span>
      </div>

      <div className="h-48 shrink-0 border-b border-ink-700">
        <CodeEditor
          ariaLabel="Test transaction payload (JSON)"
          value={payloadText}
          onChange={onPayloadTextChange}
          tone={payloadParseError ? "error" : "default"}
          placeholder='{"entity_type": "Stockbroker", "facts": {"upfront_margin_pct": 15}}'
        />
      </div>
      {payloadParseError && (
        <div className="flex items-center gap-2 border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {payloadParseError}
        </div>
      )}

      <div className="flex flex-1 flex-col gap-3 overflow-y-auto scrollbar-thin p-4">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Evaluation Result</span>
        <ResultBanner result={result} />

        {result?.violations?.length > 0 && (
          <div className="rounded-sm border border-ink-700 bg-ink-850 p-3">
            <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-slate-500">Violations</p>
            <ul className="space-y-1 text-xs text-slate-600">
              {result.violations.map((v, i) => (
                <li key={i} className="border-l-2 border-red-300 pl-2">
                  {v}
                </li>
              ))}
            </ul>
          </div>
        )}

        {result?.raw !== undefined && (
          <div className="rounded-sm border border-ink-700 bg-ink-850 p-3">
            <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-slate-500">Raw decision object</p>
            <pre className="overflow-x-auto text-xs text-slate-500">{JSON.stringify(result.raw, null, 2)}</pre>
          </div>
        )}
      </div>

      <div className="border-t border-ink-700 p-3">
        <button
          onClick={onSubmitForReview}
          disabled={!canSubmit || submitState === "submitting"}
          className="flex w-full items-center justify-center gap-2 rounded-sm border border-blue-300 bg-blue-50 px-4 py-2 text-sm font-medium text-blue-800 transition-colors hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitState === "submitting" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          Submit for HITL Review
        </button>
        {submitState === "success" && <p className="mt-2 text-center text-xs text-green-700">Submitted to the compliance review queue.</p>}
        {submitState === "error" && <p className="mt-2 text-center text-xs text-red-700">{submitError || "Submission failed."}</p>}
      </div>
    </Card>
  );
}
