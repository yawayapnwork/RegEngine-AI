import { useMemo, useState } from "react";
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
    <div className="flex h-full flex-col gap-5">
      <div className="flex items-center gap-4">
        <div className="flex gap-1 rounded-lg border border-ink-700 bg-ink-900 p-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
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

      <div className="flex-1 overflow-y-auto scrollbar-thin">
        <div className="grid grid-cols-1 gap-4 pb-6 xl:grid-cols-2">
          {visible.map((c) => (
            <HITLCaseCard
              key={c.caseId}
              hitlCase={c}
              onResolve={onResolveCase}
            />
          ))}
          {visible.length === 0 && (
            <p className="col-span-full py-16 text-center text-sm text-slate-600">
              No cases in this filter.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
