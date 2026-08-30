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
    classes: "border-green-200 bg-green-100 text-green-800",
  },
  pass: {
    icon: CheckCircle2,
    classes: "border-green-200 bg-green-100 text-green-800",
  },
  approved: {
    icon: CheckCircle2,
    classes: "border-green-200 bg-green-100 text-green-800",
  },
  compiled: {
    icon: CheckCircle2,
    classes: "border-green-200 bg-green-100 text-green-800",
  },

  in_progress: {
    icon: Loader2,
    classes: "border-blue-200 bg-blue-100 text-blue-800",
    spin: true,
  },

  pending: {
    icon: Clock,
    classes: "border-slate-200 bg-slate-100 text-slate-600",
  },

  fail: {
    icon: XCircle,
    classes: "border-red-200 bg-red-100 text-red-800",
  },
  rejected: {
    icon: XCircle,
    classes: "border-red-200 bg-red-100 text-red-800",
  },
  blocking: {
    icon: XCircle,
    classes: "border-red-200 bg-red-100 text-red-800",
  },

  hitl_blocked: {
    icon: ShieldAlert,
    classes: "border-yellow-200 bg-yellow-100 text-yellow-800",
  },
  hitl_review: {
    icon: ShieldAlert,
    classes: "border-yellow-200 bg-yellow-100 text-yellow-800",
  },
  advisory: {
    icon: AlertTriangle,
    classes: "border-yellow-200 bg-yellow-100 text-yellow-800",
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
