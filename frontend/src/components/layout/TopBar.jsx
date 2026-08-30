import { Bell, LogOut, Search, User } from "lucide-react";

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

function AuthControl({ auth0Configured, isAuthenticated, isLoading, user, onLogin, onLogout }) {
  if (!auth0Configured) {
    return (
      <span
        title="VITE_AUTH0_DOMAIN / VITE_AUTH0_CLIENT_ID are not set -- see frontend/.env.example"
        className="rounded-sm border border-amber-200 bg-amber-100 px-2 py-1 text-2xs font-semibold uppercase tracking-wide text-amber-800"
      >
        Auth not configured
      </span>
    );
  }
  if (isLoading) {
    return <div className="h-7 w-16 animate-pulse rounded-sm bg-ink-850" />;
  }
  if (!isAuthenticated) {
    return (
      <button
        onClick={onLogin}
        className="flex items-center gap-1.5 rounded-sm border border-blue-300 bg-blue-50 px-2.5 py-1.5 text-sm font-medium text-blue-800 hover:bg-blue-100"
      >
        <User className="h-3.5 w-3.5" /> Log in
      </button>
    );
  }
  return (
    <button
      onClick={onLogout}
      title={user?.email || user?.name || "Log out"}
      className="flex items-center gap-1.5 rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-1.5 text-sm font-medium text-slate-600 hover:border-red-300 hover:text-red-700"
    >
      <span className="max-w-[10rem] truncate font-mono text-2xs">
        {user?.email || user?.name || "Signed in"}
      </span>
      <LogOut className="h-3.5 w-3.5" />
    </button>
  );
}

export default function TopBar({
  activeView,
  auth0Configured = true,
  isAuthenticated = false,
  isLoading = false,
  user = null,
  onLogin = () => {},
  onLogout = () => {},
}) {
  const [title, subtitle] = TITLES[activeView] || ["", ""];
  return (
    <header className="flex items-center justify-between border-b border-ink-700 bg-ink-900 px-5 py-3">
      <div>
        <h1 className="text-sm font-semibold text-slate-900">{title}</h1>
        <p className="text-xs text-slate-500">{subtitle}</p>
      </div>
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-2 rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-1.5 text-sm text-slate-500">
          <Search className="h-3.5 w-3.5" />
          <input
            placeholder="Search transaction, clause, broker..."
            className="w-56 bg-transparent text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none"
          />
        </div>
        <button className="relative rounded-sm border border-ink-700 bg-ink-850 p-1.5 text-slate-500 hover:text-slate-800">
          <Bell className="h-3.5 w-3.5" />
          <span className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full bg-red-500" />
        </button>
        <AuthControl
          auth0Configured={auth0Configured}
          isAuthenticated={isAuthenticated}
          isLoading={isLoading}
          user={user}
          onLogin={onLogin}
          onLogout={onLogout}
        />
      </div>
    </header>
  );
}
