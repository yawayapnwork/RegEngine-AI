import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Loader2,
  ShieldAlert,
  XCircle,
} from "lucide-react";

const VARIANTS = {
  complete: {
    icon: CheckCircle2,
    classes: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  },
  pass: {
    icon: CheckCircle2,
    classes: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  },
  approved: {
    icon: CheckCircle2,
    classes: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  },
  compiled: {
    icon: CheckCircle2,
    classes: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  },

  in_progress: {
    icon: Loader2,
    classes: "border-sky-500/40 bg-sky-500/10 text-sky-400",
    spin: true,
  },

  pending: {
    icon: Clock,
    classes: "border-ink-600 bg-ink-800 text-slate-400",
  },

  fail: {
    icon: XCircle,
    classes: "border-rose-500/40 bg-rose-500/10 text-rose-400",
  },
  rejected: {
    icon: XCircle,
    classes: "border-rose-500/40 bg-rose-500/10 text-rose-400",
  },
  blocking: {
    icon: XCircle,
    classes: "border-rose-500/40 bg-rose-500/10 text-rose-400",
  },

  hitl_blocked: {
    icon: ShieldAlert,
    classes: "border-amber-500/40 bg-amber-500/10 text-amber-400",
  },
  hitl_review: {
    icon: ShieldAlert,
    classes: "border-amber-500/40 bg-amber-500/10 text-amber-400",
  },
  advisory: {
    icon: AlertTriangle,
    classes: "border-amber-500/40 bg-amber-500/10 text-amber-400",
  },
};

/** A rectangular, uppercase status tag — deliberately not a rounded/glowing
 * pill, to match the rest of this app's dense, tabular status vocabulary. */
export default function StatusBadge({ status, label }) {
  const key = String(status || "").toLowerCase();
  const variant = VARIANTS[key] || VARIANTS.pending;
  const Icon = variant.icon;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-sm border px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wide ${variant.classes}`}
    >
      <Icon className={`h-3 w-3 ${variant.spin ? "animate-spin" : ""}`} />
      {label || key.replace(/_/g, " ")}
    </span>
  );
}
