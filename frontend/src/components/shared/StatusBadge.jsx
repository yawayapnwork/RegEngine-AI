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
    classes: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  },
  pass: {
    icon: CheckCircle2,
    classes: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  },
  approved: {
    icon: CheckCircle2,
    classes: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  },
  compiled: {
    icon: CheckCircle2,
    classes: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  },

  in_progress: {
    icon: Loader2,
    classes: "bg-sky-500/10 text-sky-400 border-sky-500/30",
    spin: true,
  },

  pending: {
    icon: Clock,
    classes: "bg-slate-500/10 text-slate-400 border-slate-500/30",
  },

  fail: {
    icon: XCircle,
    classes: "bg-rose-500/10 text-rose-400 border-rose-500/30",
  },
  rejected: {
    icon: XCircle,
    classes: "bg-rose-500/10 text-rose-400 border-rose-500/30",
  },
  blocking: {
    icon: XCircle,
    classes: "bg-rose-500/10 text-rose-400 border-rose-500/30",
  },

  hitl_blocked: {
    icon: ShieldAlert,
    classes: "bg-amber-500/10 text-amber-400 border-amber-500/30",
  },
  hitl_review: {
    icon: ShieldAlert,
    classes: "bg-amber-500/10 text-amber-400 border-amber-500/30",
  },
  advisory: {
    icon: AlertTriangle,
    classes: "bg-amber-500/10 text-amber-400 border-amber-500/30",
  },
};

export default function StatusBadge({ status, label }) {
  const key = String(status || "").toLowerCase();
  const variant = VARIANTS[key] || VARIANTS.pending;
  const Icon = variant.icon;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${variant.classes}`}
    >
      <Icon className={`h-3.5 w-3.5 ${variant.spin ? "animate-spin" : ""}`} />
      {label || key.replace(/_/g, " ")}
    </span>
  );
}
