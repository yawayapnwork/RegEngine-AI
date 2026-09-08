import { FileCode2, FileText, Loader2 } from "lucide-react";
import { useState } from "react";
import Card from "../shared/Card";
import StatusBadge from "../shared/StatusBadge";
import RawTextPane from "./RawTextPane";
import RegoPane from "./RegoPane";

export default function ClauseSplitView({ clauses = [], isLoading = false }) {
  const [selectedRuleId, setSelectedRuleId] = useState(clauses[0]?.ruleId);
  const [activeIndex, setActiveIndex] = useState(null);
  const clause = clauses.find((c) => c.ruleId === selectedRuleId) ?? clauses[0];

  const selectClause = (ruleId) => {
    setSelectedRuleId(ruleId);
    setActiveIndex(null);
  };

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center">
        <Card className="max-w-md p-6">
          <Loader2 className="mx-auto mb-3 h-8 w-8 animate-spin text-blue-500" />
          <h3 className="text-base font-semibold text-slate-800">Loading Clauses...</h3>
          <p className="mt-1 text-sm text-slate-500">
            Fetching parsed circular clauses and compiled Rego rules from the backend.
          </p>
        </Card>
      </div>
    );
  }

  if (!clauses || clauses.length === 0 || !clause) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center">
        <Card className="max-w-md p-6">
          <FileText className="mx-auto mb-3 h-8 w-8 text-slate-400" />
          <h3 className="text-base font-semibold text-slate-800">No Clauses Extracted Yet</h3>
          <p className="mt-1 text-sm text-slate-500">
            Upload a SEBI PDF in the Pipeline tab to parse circular clauses, extract compliance logic, and view the compiled Rego policies.
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex h-full gap-3">
      <Card className="w-64 shrink-0 overflow-y-auto scrollbar-thin p-1.5">
        {clauses.map((c) => (
          <button
            key={c.ruleId}
            onClick={() => selectClause(c.ruleId)}
            className={`mb-0.5 flex w-full flex-col gap-1.5 border-l-2 px-2.5 py-2 text-left transition-colors ${
              c.ruleId === clause.ruleId
                ? "border-blue-500 bg-ink-800"
                : "border-transparent hover:bg-ink-850"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-sm font-medium text-slate-800">
                Clause {c.clauseNumber}
              </span>
              <StatusBadge status={c.status} />
            </div>
            <p className="line-clamp-1 text-xs text-slate-500">{c.title}</p>
          </button>
        ))}
      </Card>

      <div className="grid flex-1 grid-cols-2 gap-3 overflow-hidden">
        <Card className="flex flex-col overflow-hidden">
          <div className="flex items-center gap-2 border-b border-ink-700 bg-ink-850 px-4 py-2">
            <FileText className="h-3.5 w-3.5 text-slate-400" />
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Raw Legal Text
            </span>
            <span className="ml-auto font-mono text-2xs text-slate-500">
              source_sha256 {clause.sourceSha256.slice(0, 12)}&hellip;
            </span>
          </div>
          <div className="overflow-y-auto scrollbar-thin p-5">
            <RawTextPane
              clause={clause}
              activeIndex={activeIndex}
              onSelect={setActiveIndex}
            />
          </div>
        </Card>

        <Card className="flex flex-col overflow-hidden">
          <div className="flex items-center gap-2 border-b border-ink-700 bg-ink-850 px-4 py-2">
            <FileCode2 className="h-3.5 w-3.5 text-slate-400" />
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Compiled OPA Rego
            </span>
            <span className="ml-auto font-mono text-2xs text-slate-500">
              clause_{clause.clauseNumber.replace(/\./g, "_")}
            </span>
          </div>
          <div className="overflow-y-auto scrollbar-thin py-4">
            <RegoPane clause={clause} activeIndex={activeIndex} />
          </div>
        </Card>
      </div>
    </div>
  );
}
