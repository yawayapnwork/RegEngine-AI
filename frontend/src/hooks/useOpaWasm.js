import { useCallback, useMemo, useState } from "react";
import { loadPolicy } from "@open-policy-agent/opa-wasm";
import { evaluateJsonLogic } from "../lib/jsonLogicEvaluator";

// Requirement 2 asks for "zero-latency feedback as users modify rules"
// via OPA running as WebAssembly directly in the browser. The real
// `@open-policy-agent/opa-wasm` package (verified against its
// published README/source -- loadPolicy(bytes) -> Promise<LoadedPolicy>,
// policy.evaluate(input) -> ResultSet | null, policy.setData(data))
// only runs a policy that has ALREADY been compiled to `.wasm` via the
// `opa build -t wasm` CLI -- which cannot run inside a browser tab, so
// there is no way to compile freshly-typed Rego text to Wasm client-side.
//
// This hook is therefore genuinely two evaluation engines behind one
// interface, and always tells the caller which one just ran:
//   - "opa-wasm": a real compiled `.wasm` policy bundle was loaded (via
//     `loadWasmBundle`, e.g. fetched from a backend that already ran
//     `opa build` on a published rule) and is evaluated with the real
//     OPA Wasm runtime -- bit-for-bit the same engine production OPA
//     uses, just running client-side.
//   - "json-logic": no compiled bundle is loaded (the common case while
//     someone is actively editing a rule that hasn't been compiled yet),
//     so the middle pane's JSON-Logic AST is evaluated with the
//     dependency-free evaluator in lib/jsonLogicEvaluator.js -- true
//     zero-latency, zero-network feedback, using the exact grammar
//     app.compiler.jsonlogic_compiler emits.
export function useOpaWasm() {
  const [wasmPolicy, setWasmPolicy] = useState(null);
  const [wasmMeta, setWasmMeta] = useState(null); // { fileName, entrypoints }
  const [wasmStatus, setWasmStatus] = useState("idle"); // idle | loading | ready | error
  const [wasmError, setWasmError] = useState(null);

  const loadWasmBundle = useCallback(async (file) => {
    setWasmStatus("loading");
    setWasmError(null);
    try {
      const bytes = await file.arrayBuffer();
      const policy = await loadPolicy(bytes);
      setWasmPolicy(policy);
      setWasmMeta({ fileName: file.name, entrypoints: Object.keys(policy.entrypoints || {}) });
      setWasmStatus("ready");
    } catch (err) {
      setWasmPolicy(null);
      setWasmMeta(null);
      setWasmStatus("error");
      setWasmError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const clearWasmBundle = useCallback(() => {
    setWasmPolicy(null);
    setWasmMeta(null);
    setWasmStatus("idle");
    setWasmError(null);
  }, []);

  /** `inputDoc` is the `{ entity_type, facts }` document both engines
   * share. `jsonLogicAst` is only used by the fallback engine. Returns
   * `{ engine, allow, violations, raw, error }` -- never throws;
   * evaluation failures (malformed AST, a missing fact) come back as
   * `error` so the UI can render them inline instead of crashing the
   * playground. */
  const evaluate = useCallback(
    (inputDoc, jsonLogicAst) => {
      if (wasmPolicy) {
        try {
          const resultSet = wasmPolicy.evaluate(inputDoc);
          if (resultSet == null) {
            return { engine: "opa-wasm", error: "OPA Wasm evaluation returned no result (undefined)." };
          }
          if (resultSet.length === 0) {
            return { engine: "opa-wasm", error: "OPA Wasm evaluation was undefined for this input." };
          }
          const decision = resultSet[0].result;
          // `opa build -t wasm -e <pkg>/decision` bundles expose the full
          // decision object (see app.compiler.rego_compiler's `decision`
          // rule); a bundle built against `.../allow` exposes a bare
          // boolean instead -- support both shapes.
          if (typeof decision === "boolean") {
            return { engine: "opa-wasm", allow: decision, violations: [], raw: decision };
          }
          return {
            engine: "opa-wasm",
            allow: Boolean(decision?.allow),
            violations: decision?.violations || [],
            raw: decision,
          };
        } catch (err) {
          return { engine: "opa-wasm", error: err instanceof Error ? err.message : String(err) };
        }
      }

      try {
        const satisfied = Boolean(evaluateJsonLogic(jsonLogicAst, inputDoc));
        return { engine: "json-logic", allow: satisfied, violations: satisfied ? [] : ["JSON-Logic AST evaluated to false for this input."], raw: satisfied };
      } catch (err) {
        return { engine: "json-logic", error: err instanceof Error ? err.message : String(err) };
      }
    },
    [wasmPolicy],
  );

  return useMemo(
    () => ({
      evaluate,
      loadWasmBundle,
      clearWasmBundle,
      wasmStatus,
      wasmError,
      wasmMeta,
      hasWasmPolicy: Boolean(wasmPolicy),
    }),
    [evaluate, loadWasmBundle, clearWasmBundle, wasmStatus, wasmError, wasmMeta, wasmPolicy],
  );
}
