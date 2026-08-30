import { useMemo, useState } from "react";
import Card from "../shared/Card";
import HITLCaseCard from "./HITLCaseCard";

const FILTERS = [
  { id: "pending", label: "Pending" },
  { id: "resolved", label: "Resolved" },
  { id: "all", label: "All" },
];

export default function HITLDashboard({ cases, onResolveCase }) {
  const [filter, setFilter] = useState("pending");

  const visible = useMemo(() => {
    if (filter === "all") return cases;
    if (filter === "pending")
      return cases.filter((c) => c.status === "pending");
    return cases.filter((c) => c.status !== "pending");
  }, [cases, filter]);

  const pendingCount = cases.filter((c) => c.status === "pending").length;

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center gap-4">
        <div className="flex gap-0.5 rounded-sm border border-ink-700 bg-ink-900 p-0.5">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`rounded-sm px-3 py-1.5 text-sm font-medium transition-colors ${
                filter === f.id
                  ? "bg-ink-700 text-slate-100"
                  : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <p className="text-sm text-slate-500">
          <span className="font-semibold text-amber-400">{pendingCount}</span>{" "}
          case{pendingCount === 1 ? "" : "s"} awaiting compliance-officer
          sign-off
        </p>
      </div>

      <Card className="flex-1 overflow-hidden">
        <div className="grid grid-cols-[1fr_1.6fr_1.4fr_auto] gap-4 border-b border-ink-700 bg-ink-850 px-4 py-2 text-2xs font-semibold uppercase tracking-wide text-slate-500">
          <span>Case</span>
          <span>Conflict / Diff</span>
          <span>Audit Note</span>
          <span className="text-right">Action</span>
        </div>
        <div className="h-[calc(100%-2.25rem)] divide-y divide-ink-800 overflow-y-auto scrollbar-thin">
          {visible.map((c) => (
            <HITLCaseCard
              key={c.caseId}
              hitlCase={c}
              onResolve={onResolveCase}
            />
          ))}
          {visible.length === 0 && (
            <p className="py-16 text-center text-sm text-slate-600">
              No cases in this filter.
            </p>
          )}
        </div>
      </Card>
    </div>
  );
}
