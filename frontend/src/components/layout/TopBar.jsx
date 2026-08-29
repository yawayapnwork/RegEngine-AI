import { Bell, Search } from "lucide-react";

const TITLES = {
  pipeline: [
    "Ingestion Pipeline",
    "Track a SEBI circular from PDF upload through Rego compilation.",
  ],
  splitview: [
    "Clause / Rego Split View",
    "Raw legal text against its compiled OPA policy, clause by clause.",
  ],
  playground: [
    "Policy Playground",
    "Edit a policy, run it instantly against a test transaction via OPA Wasm, and submit it for compliance sign-off.",
  ],
  hitl: [
    "HITL Compliance Review",
    "Human sign-off queue for ambiguous compiler and execution-time cases.",
  ],
  vault: [
    "Transaction Audit Vault",
    "Live broker transactions, chained to the SEBI clause hashes that decided them.",
  ],
};

export default function TopBar({ activeView }) {
  const [title, subtitle] = TITLES[activeView] || ["", ""];
  return (
    <header className="flex items-center justify-between border-b border-ink-700 bg-ink-900/60 px-6 py-4">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">{title}</h1>
        <p className="text-sm text-slate-500">{subtitle}</p>
      </div>
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 rounded-lg border border-ink-700 bg-ink-850 px-3 py-1.5 text-sm text-slate-400">
          <Search className="h-4 w-4" />
          <input
            placeholder="Search transaction, clause, broker..."
            className="w-56 bg-transparent text-slate-200 placeholder:text-slate-600 focus:outline-none"
          />
        </div>
        <button className="relative rounded-lg border border-ink-700 bg-ink-850 p-2 text-slate-400 hover:text-slate-200">
          <Bell className="h-4 w-4" />
          <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-rose-500" />
        </button>
        <div className="h-8 w-8 rounded-full bg-gradient-to-br from-sky-400 to-indigo-500" />
      </div>
    </header>
  );
}
