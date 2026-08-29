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
    <div className="flex h-full gap-4">
      <Card className="w-56 shrink-0 overflow-y-auto scrollbar-thin p-2">
        {clauses.map((c) => (
          <button
            key={c.ruleId}
            onClick={() => onSelectClause(c.ruleId)}
            className={`mb-1 flex w-full flex-col gap-1.5 rounded-lg px-3 py-2.5 text-left transition-colors ${
              c.ruleId === clause.ruleId ? "bg-ink-800 ring-1 ring-inset ring-ink-600" : "hover:bg-ink-800/60"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium text-slate-200">Clause {c.clauseNumber}</span>
              <StatusBadge status={c.status} />
            </div>
            <p className="line-clamp-1 text-xs text-slate-500">{c.title}</p>
          </button>
        ))}
      </Card>

      <Card className="flex flex-1 flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b border-ink-700 px-4 py-3">
          <FileText className="h-4 w-4 text-slate-400" />
          <span className="text-sm font-medium text-slate-300">Raw SEBI Circular Text</span>
          <span className="ml-auto text-xs text-slate-500">{clause.circularNumber}</span>
        </div>
        <div className="overflow-y-auto scrollbar-thin p-5">
          <RawTextPane clause={clause} activeIndex={activeIndex} onSelect={onSelectHighlight} />
        </div>
      </Card>
    </div>
  );
}
