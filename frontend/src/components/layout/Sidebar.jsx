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
    <aside className="flex w-60 shrink-0 flex-col border-r border-ink-700 bg-ink-900 px-2 py-4">
      <div className="mb-6 flex items-center gap-2 px-2">
        <ScrollText className="h-5 w-5 text-sky-400" />
        <div>
          <p className="text-sm font-semibold leading-none text-slate-100">
            RegEngine AI
          </p>
          <p className="mt-1 text-2xs uppercase tracking-wide text-slate-500">
            Compliance Control Plane
          </p>
        </div>
      </div>

      <nav className="flex flex-col gap-0.5">
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => {
          const active = activeView === id;
          return (
            <button
              key={id}
              onClick={() => onNavigate(id)}
              className={`flex items-center justify-between border-l-2 px-2.5 py-2 text-sm transition-colors ${
                active
                  ? "border-sky-500 bg-ink-800 text-slate-100"
                  : "border-transparent text-slate-400 hover:bg-ink-850 hover:text-slate-200"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <Icon className="h-3.5 w-3.5" />
                {label}
              </span>
              {id === "hitl" && pendingHitlCount > 0 && (
                <span className="rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-2xs font-semibold text-amber-400">
                  {pendingHitlCount}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div className="mt-auto rounded-sm border border-ink-700 bg-ink-850 p-3 text-2xs text-slate-500">
        <p className="mb-1.5 font-semibold uppercase tracking-wide text-slate-500">
          Engine status
        </p>
        <p className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
          OPA server: healthy
        </p>
        <p className="mt-1 flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
          Ledger chain: intact
        </p>
      </div>
    </aside>
  );
}
