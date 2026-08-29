import {
  FileStack,
  FlaskConical,
  GitCompare,
  ScrollText,
  ShieldCheck,
  Vault,
} from "lucide-react";

const NAV_ITEMS = [
  { id: "pipeline", label: "Ingestion Pipeline", icon: FileStack },
  { id: "splitview", label: "Clause / Rego Split View", icon: GitCompare },
  { id: "playground", label: "Policy Playground", icon: FlaskConical },
  { id: "hitl", label: "HITL Compliance Review", icon: ShieldCheck },
  { id: "vault", label: "Transaction Audit Vault", icon: Vault },
];

export default function Sidebar({ activeView, onNavigate, pendingHitlCount }) {
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-ink-700 bg-ink-900/80 px-4 py-6">
      <div className="mb-8 flex items-center gap-2 px-2">
        <ScrollText className="h-6 w-6 text-sky-400" />
        <div>
          <p className="text-sm font-semibold text-slate-100">RegEngine AI</p>
          <p className="text-xs text-slate-500">Compliance Control Plane</p>
        </div>
      </div>

      <nav className="flex flex-col gap-1">
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => {
          const active = activeView === id;
          return (
            <button
              key={id}
              onClick={() => onNavigate(id)}
              className={`flex items-center justify-between rounded-lg px-3 py-2.5 text-sm transition-colors ${
                active
                  ? "bg-sky-500/10 text-sky-300 ring-1 ring-inset ring-sky-500/30"
                  : "text-slate-400 hover:bg-ink-800 hover:text-slate-200"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <Icon className="h-4 w-4" />
                {label}
              </span>
              {id === "hitl" && pendingHitlCount > 0 && (
                <span className="rounded-full bg-amber-500/20 px-1.5 py-0.5 text-[11px] font-semibold text-amber-400">
                  {pendingHitlCount}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div className="mt-auto rounded-lg border border-ink-700 bg-ink-850 p-3 text-xs text-slate-500">
        <p className="mb-1 font-medium text-slate-400">Engine status</p>
        <p className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> OPA
          server: healthy
        </p>
        <p className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> Ledger
          chain: intact
        </p>
      </div>
    </aside>
  );
}
