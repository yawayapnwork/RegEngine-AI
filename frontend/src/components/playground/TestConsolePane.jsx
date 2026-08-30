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
      <div className="flex items-start gap-2 rounded-sm border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-sm text-amber-300">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <div>
          <p className="font-medium">Evaluation error</p>
          <p className="text-xs text-amber-400/90">{result.error}</p>
        </div>
      </div>
    );
  }
  const allow = Boolean(result.allow);
  return (
    <div
      className={`flex items-center gap-2 rounded-sm border-l-2 border px-3 py-2.5 text-sm font-semibold uppercase tracking-wide ${
        allow ? "border-emerald-500 border-y-emerald-500/30 border-r-emerald-500/30 bg-emerald-500/10 text-emerald-400" : "border-rose-500 border-y-rose-500/30 border-r-rose-500/30 bg-rose-500/10 text-rose-400"
      }`}
    >
      {allow ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
      {allow ? "ALLOW" : "DENY"}
      <span className="ml-auto rounded-sm bg-black/20 px-2 py-0.5 text-2xs font-medium normal-case tracking-normal text-slate-300">
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
        <span className="text-sm font-medium text-slate-300">Test Transaction Payload</span>
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
        <div className="flex items-center gap-2 border-b border-rose-500/30 bg-rose-500/10 px-4 py-2 text-xs text-rose-400">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {payloadParseError}
        </div>
      )}

      <div className="flex flex-1 flex-col gap-3 overflow-y-auto scrollbar-thin p-4">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Evaluation Result</span>
        <ResultBanner result={result} />

        {result?.violations?.length > 0 && (
          <div className="rounded-sm border border-ink-700 bg-ink-850 p-3">
            <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-slate-500">Violations</p>
            <ul className="space-y-1 text-xs text-slate-400">
              {result.violations.map((v, i) => (
                <li key={i} className="border-l-2 border-rose-500/40 pl-2">
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
          className="flex w-full items-center justify-center gap-2 rounded-sm border border-sky-500/40 bg-sky-500/10 px-4 py-2 text-sm font-medium text-sky-300 transition-colors hover:bg-sky-500/20 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitState === "submitting" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          Submit for HITL Review
        </button>
        {submitState === "success" && <p className="mt-2 text-center text-xs text-emerald-400">Submitted to the compliance review queue.</p>}
        {submitState === "error" && <p className="mt-2 text-center text-xs text-rose-400">{submitError || "Submission failed."}</p>}
      </div>
    </Card>
  );
}
