import { AlertTriangle, Cpu, FileCode2, Upload, X, Zap } from "lucide-react";
import { useRef } from "react";
import Card from "../shared/Card";
import CodeEditor from "./CodeEditor";

const TABS = [
  { id: "rego", label: "OPA Rego" },
  { id: "jsonlogic", label: "JSON-Logic AST" },
];

/** Middle panel (Requirement 1 + 2). Two editable representations of the
 * same policy:
 *   - "OPA Rego": the human-authored/compiled Rego module. Editable so
 *     a reviewer can sketch a fix, but NOT locally executable -- turning
 *     edited Rego text into something evaluable requires the `opa`
 *     compiler toolchain, which cannot run in a browser tab (see
 *     hooks/useOpaWasm.js's module docstring). Loading a pre-compiled
 *     `.wasm` bundle (built server-side from this exact Rego) is how
 *     Requirement 2's real OPA Wasm evaluation gets wired up here.
 *   - "JSON-Logic AST": the same policy as a JSON-Logic tree
 *     (app.compiler.jsonlogic_compiler's output shape). This one IS
 *     locally, instantly evaluable client-side (lib/jsonLogicEvaluator.js)
 *     with no compile step, so it's what actually drives the right
 *     panel's live "as you type" result when no compiled Wasm bundle is
 *     loaded.
 */
export default function PolicyEditorPane({
  activeTab,
  onTabChange,
  regoValue,
  onRegoChange,
  jsonLogicText,
  onJsonLogicTextChange,
  jsonLogicParseError,
  wasm,
}) {
  const fileInputRef = useRef(null);

  return (
    <Card className="flex flex-col overflow-hidden">
      <div className="flex items-center gap-1 overflow-x-auto border-b border-ink-700 bg-ink-850 px-2 py-1.5">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={`flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm px-2.5 py-1 text-sm font-medium transition-colors ${
              activeTab === tab.id ? "bg-ink-700 text-slate-100" : "text-slate-500 hover:text-slate-300"
            }`}
          >
            <FileCode2 className="h-3.5 w-3.5" />
            {tab.label}
          </button>
        ))}
      </div>

      <div className="flex items-center gap-2 border-b border-ink-700 px-2 py-1.5">
        {wasm.hasWasmPolicy ? (
          <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 text-2xs font-semibold uppercase tracking-wide text-emerald-400">
            <Cpu className="h-3 w-3" /> Wasm loaded ({wasm.wasmMeta?.fileName})
            <button onClick={wasm.clearWasmBundle} className="ml-1 text-emerald-400/70 hover:text-emerald-200">
              <X className="h-3 w-3" />
            </button>
          </span>
        ) : (
          <span className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm border border-sky-500/40 bg-sky-500/10 px-2 py-1 text-2xs font-semibold uppercase tracking-wide text-sky-400">
            <Zap className="h-3 w-3" /> JSON-Logic (instant)
          </span>
        )}
        <input
          ref={fileInputRef}
          type="file"
          accept=".wasm"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) wasm.loadWasmBundle(file);
            e.target.value = "";
          }}
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          title="Load a pre-compiled OPA .wasm bundle (opa build -t wasm) for real OPA Wasm evaluation"
          className="ml-auto flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm border border-ink-700 px-2 py-1 text-xs font-medium text-slate-400 hover:border-ink-600 hover:text-slate-200"
        >
          <Upload className="h-3.5 w-3.5" /> Load .wasm
        </button>
      </div>

      {wasm.wasmStatus === "loading" && (
        <div className="border-b border-ink-700 bg-ink-850 px-4 py-2 text-xs text-slate-400">Loading compiled Wasm policy...</div>
      )}
      {wasm.wasmStatus === "error" && (
        <div className="flex items-center gap-2 border-b border-rose-500/30 bg-rose-500/10 px-4 py-2 text-xs text-rose-400">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> Failed to load Wasm bundle: {wasm.wasmError}
        </div>
      )}

      <div className="min-h-0 flex-1">
        {activeTab === "rego" ? (
          <CodeEditor
            ariaLabel="Rego source editor"
            value={regoValue ?? ""}
            onChange={onRegoChange}
            placeholder="No compiled Rego for this clause -- sketch a policy here (not locally executable; publish to see it enforced)."
          />
        ) : (
          <CodeEditor
            ariaLabel="JSON-Logic AST editor"
            value={jsonLogicText}
            onChange={onJsonLogicTextChange}
            tone={jsonLogicParseError ? "error" : "default"}
            placeholder='{"and": [{">=": [{"var": "facts.upfront_margin_pct"}, 20]}]}'
          />
        )}
      </div>

      {activeTab === "jsonlogic" && jsonLogicParseError && (
        <div className="flex items-center gap-2 border-t border-rose-500/30 bg-rose-500/10 px-4 py-2 text-xs text-rose-400">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {jsonLogicParseError}
        </div>
      )}
    </Card>
  );
}
