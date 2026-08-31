import { useState } from "react";
import { Lock, LogIn, ShieldCheck } from "lucide-react";

export default function LoginPage({ onLogin, isLoading = false, error = null }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!email || !password) return;
    onLogin(email, password);
  };

  return (
    <div className="flex h-screen items-center justify-center bg-ink-950 px-4">
      <div className="w-full max-w-sm rounded-md border border-ink-700 bg-ink-900 p-6 shadow-lg">
        <div className="mb-6 flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 text-blue-500" />
          <div>
            <h1 className="text-sm font-semibold text-slate-900">RegEngine AI</h1>
            <p className="text-xs text-slate-500">Sign in to the compliance dashboard.</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label htmlFor="email" className="mb-1 block text-xs font-medium text-slate-600">
              Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@compliance"
              className="w-full rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-1.5 text-sm text-slate-800 placeholder:text-slate-400 focus:border-blue-400 focus:outline-none"
            />
          </div>
          <div>
            <label htmlFor="password" className="mb-1 block text-xs font-medium text-slate-600">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              className="w-full rounded-sm border border-ink-700 bg-ink-850 px-2.5 py-1.5 text-sm text-slate-800 placeholder:text-slate-400 focus:border-blue-400 focus:outline-none"
            />
          </div>

          {error && (
            <div className="rounded-sm border border-red-300 bg-red-50 px-2.5 py-1.5 text-xs text-red-700">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={isLoading}
            className="flex w-full items-center justify-center gap-1.5 rounded-sm border border-blue-300 bg-blue-50 px-2.5 py-2 text-sm font-medium text-blue-800 hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isLoading ? (
              <>
                <Lock className="h-3.5 w-3.5 animate-pulse" /> Signing in...
              </>
            ) : (
              <>
                <LogIn className="h-3.5 w-3.5" /> Log in
              </>
            )}
          </button>
        </form>

        <p className="mt-4 text-2xs text-slate-400">
          Accounts are provisioned by a System Admin. Contact your administrator if you don't have credentials.
        </p>
      </div>
    </div>
  );
}
