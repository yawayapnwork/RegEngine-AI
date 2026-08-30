import { FileText } from "lucide-react";
import Card from "../shared/Card";
import StatusBadge from "../shared/StatusBadge";
import RawTextPane from "../splitview/RawTextPane";

/** Left panel (Requirement 1): the clause picker rail + the raw SEBI
 * circular text for whichever clause is selected. Reuses
 * splitview/RawTextPane as-is (same `[[marked]]`-span highlight
 * behavior as the Clause/Rego Split View) so a highlighted threshold in
 * the source text and the corresponding line in the middle editor pane
 * can eventually share the same `activeIndex` wiring PolicyEditorPane
 * already anchors against, exactly like ClauseSplitView does today. */
export default function CircularTextPane({ clauses, selectedRuleId, onSelectClause, activeIndex, onSelectHighlight }) {
  const clause = clauses.find((c) => c.ruleId === selectedRuleId) ?? clauses[0];

  return (
    <div className="flex h-full gap-3">
      <Card className="w-48 shrink-0 overflow-y-auto scrollbar-thin p-1.5">
        {clauses.map((c) => (
          <button
            key={c.ruleId}
            onClick={() => onSelectClause(c.ruleId)}
            className={`mb-0.5 flex w-full flex-col gap-1.5 border-l-2 px-2.5 py-2 text-left transition-colors ${
              c.ruleId === clause.ruleId ? "border-sky-500 bg-ink-800" : "border-transparent hover:bg-ink-850"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-sm font-medium text-slate-200">Clause {c.clauseNumber}</span>
              <StatusBadge status={c.status} />
            </div>
            <p className="line-clamp-1 text-xs text-slate-500">{c.title}</p>
          </button>
        ))}
      </Card>

      <Card className="flex flex-1 flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b border-ink-700 bg-ink-850 px-4 py-2">
          <FileText className="h-3.5 w-3.5 text-slate-500" />
          <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Raw SEBI Circular Text</span>
          <span className="ml-auto font-mono text-2xs text-slate-500">{clause.circularNumber}</span>
        </div>
        <div className="overflow-y-auto scrollbar-thin p-5">
          <RawTextPane clause={clause} activeIndex={activeIndex} onSelect={onSelectHighlight} />
        </div>
      </Card>
    </div>
  );
}
