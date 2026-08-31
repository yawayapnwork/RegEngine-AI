import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2, Lock, ShieldCheck, X } from "lucide-react";

const ORG_TYPES = [
  { value: "stockbroker", label: "Stockbroker" },
  { value: "amc", label: "AMC" },
  { value: "ia", label: "Investment Adviser" },
  { value: "other", label: "Other" },
];

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function fieldError(mode, values) {
  if (!values.email) return "Work email is required.";
  if (!EMAIL_RE.test(values.email)) return "Enter a valid email address.";
  if (!values.password) return "Password is required.";
  if (mode === "signup") {
    if (!values.fullName.trim()) return "Full name is required.";
    if (values.password.length < 8) return "Password must be at least 8 characters.";
    if (values.password !== values.confirmPassword) return "Passwords do not match.";
  }
  return null;
}

export default function AuthModal({
  isOpen,
  initialMode = "login",
  onClose,
  onLogin,
  onSignup,
  isLoading = false,
  error = null,
}) {
  const [mode, setMode] = useState(initialMode);
  const [values, setValues] = useState({
    fullName: "",
    email: "",
    password: "",
    confirmPassword: "",
    orgType: ORG_TYPES[0].value,
    rememberMe: true,
  });
  const [localError, setLocalError] = useState(null);
  const [signupNotice, setSignupNotice] = useState(null);

  // Reset to the tab the caller asked for (e.g. Header's "Log In" vs
  // "Get Started" buttons) each time the modal is (re)opened, rather than
  // carrying over whatever tab it was left on last time it closed.
  useEffect(() => {
    if (isOpen) {
      setMode(initialMode);
      setLocalError(null);
      setSignupNotice(null);
    }
  }, [isOpen, initialMode]);

  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const setField = (key) => (e) => {
    const val = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    setValues((prev) => ({ ...prev, [key]: val }));
  };

  const switchMode = (next) => {
    setMode(next);
    setLocalError(null);
    setSignupNotice(null);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const validation = fieldError(mode, values);
    if (validation) {
      setLocalError(validation);
      return;
    }
    setLocalError(null);

    if (mode === "signup") {
      // The backend's POST /v1/auth/signup only accepts email + password
      // (app.security.models.LoginRequest) -- fullName/orgType are
      // collected here for the account-creation UX and future use, but
      // aren't persisted anywhere yet.
      const ok = await onSignup({
        email: values.email,
        password: values.password,
        fullName: values.fullName,
        orgType: values.orgType,
      });
      if (ok) {
        setSignupNotice("Account created. Sign in below to continue.");
        setMode("login");
        setValues((prev) => ({ ...prev, password: "", confirmPassword: "" }));
      }
      return;
    }

    onLogin(values.email, values.password, values.rememberMe);
  };

  const displayedError = localError || error;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4 backdrop-blur-sm"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-7 shadow-2xl">
        <div className="mb-6 flex items-start justify-between">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-blue-800" />
            <div>
              <h2 className="text-sm font-semibold text-slate-900">RegEngine AI</h2>
              <p className="text-xs text-slate-500">
                {mode === "login" ? "Sign in to your compliance dashboard." : "Create a Compliance Officer account."}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mb-6 grid grid-cols-2 rounded-lg border border-slate-200 bg-slate-50 p-1 text-sm font-medium">
          <button
            type="button"
            onClick={() => switchMode("login")}
            className={`rounded-md py-1.5 transition-colors ${
              mode === "login" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
            }`}
          >
            Sign In
          </button>
          <button
            type="button"
            onClick={() => switchMode("signup")}
            className={`rounded-md py-1.5 transition-colors ${
              mode === "signup" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
            }`}
          >
            Create Account
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3.5">
          {mode === "signup" && (
            <div>
              <label htmlFor="fullName" className="mb-1 block text-xs font-medium text-slate-600">
                Full Name
              </label>
              <input
                id="fullName"
                type="text"
                autoComplete="name"
                required
                value={values.fullName}
                onChange={setField("fullName")}
                placeholder="Jane Sharma"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
            </div>
          )}

          <div>
            <label htmlFor="email" className="mb-1 block text-xs font-medium text-slate-600">
              Work Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              required
              value={values.email}
              onChange={setField("email")}
              placeholder="you@brokerage.com"
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          {mode === "signup" && (
            <div>
              <label htmlFor="orgType" className="mb-1 block text-xs font-medium text-slate-600">
                Organization Type
              </label>
              <select
                id="orgType"
                value={values.orgType}
                onChange={setField("orgType")}
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                {ORG_TYPES.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label htmlFor="password" className="mb-1 block text-xs font-medium text-slate-600">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              required
              minLength={mode === "signup" ? 8 : undefined}
              value={values.password}
              onChange={setField("password")}
              placeholder="••••••••"
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          {mode === "signup" && (
            <div>
              <label htmlFor="confirmPassword" className="mb-1 block text-xs font-medium text-slate-600">
                Confirm Password
              </label>
              <input
                id="confirmPassword"
                type="password"
                autoComplete="new-password"
                required
                value={values.confirmPassword}
                onChange={setField("confirmPassword")}
                placeholder="••••••••"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
            </div>
          )}

          {mode === "login" && (
            <label className="flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={values.rememberMe}
                onChange={setField("rememberMe")}
                className="h-3.5 w-3.5 rounded border-slate-300 text-blue-700 focus:ring-blue-500"
              />
              Remember me on this device
            </label>
          )}

          {signupNotice && (
            <div className="flex items-start gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {signupNotice}
            </div>
          )}
          {displayedError && (
            <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {displayedError}
            </div>
          )}

          <button
            type="submit"
            disabled={isLoading}
            className="flex w-full items-center justify-center gap-2 rounded-md bg-blue-800 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-blue-900 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isLoading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                {mode === "login" ? "Signing in..." : "Creating account..."}
              </>
            ) : mode === "login" ? (
              <>
                <Lock className="h-4 w-4" /> Sign In
              </>
            ) : (
              "Create Account"
            )}
          </button>
        </form>

        <button
          type="button"
          onClick={() => switchMode(mode === "login" ? "signup" : "login")}
          className="mt-4 w-full text-center text-xs text-slate-500 hover:text-blue-800 hover:underline"
        >
          {mode === "login" ? "Need an account? Create one." : "Already have an account? Sign in."}
        </button>
      </div>
    </div>
  );
}
