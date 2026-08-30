import { useEffect, useMemo, useState } from "react";
import { useOpaWasm } from "../../hooks/useOpaWasm";
import CircularTextPane from "./CircularTextPane";
import PolicyEditorPane from "./PolicyEditorPane";
import TestConsolePane from "./TestConsolePane";

function safeJsonParse(text) {
  try {
    return { value: JSON.parse(text), error: null };
  } catch (err) {
    return { value: null, error: err instanceof Error ? err.message : "Invalid JSON." };
  }
}

/** The Policy Playground IDE (Requirements 1-3): a 3-panel Rego
 * Playground-style workspace embedded in the compliance dashboard.
 *
 *   Left    -- CircularTextPane: raw SEBI circular text for the
 *              selected clause.
 *   Middle  -- PolicyEditorPane: the editable Rego / JSON-Logic AST,
 *              plus the OPA Wasm bundle loader.
 *   Right   -- TestConsolePane: the editable test transaction payload,
 *              the real-time ALLOW/DENY result, and "Submit for HITL
 *              Review".
 *
 * Evaluation re-runs on every keystroke in either the JSON-Logic editor
 * or the payload editor -- both are pure, synchronous, in-memory
 * operations (real OPA Wasm exports and the JS JSON-Logic evaluator
 * alike), so there is no debounce here; that IS the "zero-latency
 * feedback" Requirement 2 asks for.
 */
export default function PolicyPlayground({ clauses, onSubmitForReview }) {
  const [selectedRuleId, setSelectedRuleId] = useState(clauses[0]?.ruleId);
  const [activeHighlightIndex, setActiveHighlightIndex] = useState(null);
  const [editorTab, setEditorTab] = useState("jsonlogic");
  const [regoText, setRegoText] = useState("");
  const [jsonLogicText, setJsonLogicText] = useState("");
  const [payloadText, setPayloadText] = useState("");
  const [submitState, setSubmitState] = useState("idle"); // idle | submitting | success | error
  const [submitError, setSubmitError] = useState(null);

  const wasm = useOpaWasm();
  const clause = clauses.find((c) => c.ruleId === selectedRuleId) ?? clauses[0];

  // Reset the editors from the newly-selected clause's compiled output.
  useEffect(() => {
    setRegoText(clause.regoCode || "");
    setJsonLogicText(clause.jsonLogic ? JSON.stringify(clause.jsonLogic, null, 2) : "");
    setPayloadText(JSON.stringify(clause.sampleTransaction || { entity_type: "Stockbroker", facts: {} }, null, 2));
    setSubmitState("idle");
    setSubmitError(null);
    wasm.clearWasmBundle();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clause.ruleId]);

  const parsedJsonLogic = useMemo(() => safeJsonParse(jsonLogicText || "null"), [jsonLogicText]);
  const parsedPayload = useMemo(() => safeJsonParse(payloadText || "null"), [payloadText]);

  const result = useMemo(() => {
    if (parsedPayload.error) return { error: `Payload is not valid JSON: ${parsedPayload.error}` };
    if (!wasm.hasWasmPolicy && parsedJsonLogic.error) return { error: `JSON-Logic AST is not valid JSON: ${parsedJsonLogic.error}` };
    if (!wasm.hasWasmPolicy && parsedJsonLogic.value === null) return { error: "No JSON-Logic AST to evaluate for this clause yet." };
    return wasm.evaluate(parsedPayload.value, parsedJsonLogic.value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parsedPayload, parsedJsonLogic, wasm.hasWasmPolicy, wasm.evaluate]);

  const canSubmit = !parsedPayload.error && (regoText.trim() || jsonLogicText.trim());

  const handleSubmitForReview = async () => {
    setSubmitState("submitting");
    setSubmitError(null);
    try {
      await onSubmitForReview({
        ruleId: clause.ruleId,
        clauseNumber: clause.clauseNumber,
        circularNumber: clause.circularNumber,
        editedRego: regoText || null,
        editedJsonLogic: parsedJsonLogic.error ? null : parsedJsonLogic.value,
        lastEvaluation: result?.error ? null : { engine: result?.engine, allow: result?.allow, testPayload: parsedPayload.value },
      });
      setSubmitState("success");
    } catch (err) {
      setSubmitState("error");
      setSubmitError(err instanceof Error ? err.message : "Submission failed.");
    }
  };

  return (
    <div className="grid h-full grid-cols-[1.1fr_1fr_0.9fr] gap-3">
      <CircularTextPane
        clauses={clauses}
        selectedRuleId={clause.ruleId}
        onSelectClause={setSelectedRuleId}
        activeIndex={activeHighlightIndex}
        onSelectHighlight={setActiveHighlightIndex}
      />
      <PolicyEditorPane
        activeTab={editorTab}
        onTabChange={setEditorTab}
        regoValue={regoText}
        onRegoChange={setRegoText}
        jsonLogicText={jsonLogicText}
        onJsonLogicTextChange={setJsonLogicText}
        jsonLogicParseError={jsonLogicText.trim() ? parsedJsonLogic.error : null}
        wasm={wasm}
      />
      <TestConsolePane
        payloadText={payloadText}
        onPayloadTextChange={setPayloadText}
        payloadParseError={parsedPayload.error}
        result={result}
        submitState={submitState}
        submitError={submitError}
        onSubmitForReview={handleSubmitForReview}
        canSubmit={canSubmit}
      />
    </div>
  );
}
