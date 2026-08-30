import { Link2 } from "lucide-react";
import StatusBadge from "../shared/StatusBadge";

function truncateHash(hash) {
  return hash?.length > 16 ? `${hash.slice(0, 8)}…${hash.slice(-6)}` : hash;
}

export default function TransactionRow({ entry, isNew }) {
  return (
    <tr
      className={`border-b border-ink-700 text-sm transition-colors even:bg-ink-850 ${isNew ? "bg-blue-50" : ""}`}
    >
      <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs tabular-nums text-slate-500">
        #{entry.sequenceNum}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs text-slate-800">
        {entry.transactionId}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs text-slate-600">
        {entry.brokerId}
      </td>
      <td className="whitespace-nowrap px-4 py-2.5 font-mono text-xs text-slate-500">
        {new Date(entry.evaluatedAt).toLocaleTimeString()}
      </td>
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-1.5">
          <Link2 className="h-3.5 w-3.5 shrink-0 text-slate-400" />
          <span
            className="font-mono text-xs text-slate-600"
            title={entry.clauseHash}
          >
            {truncateHash(entry.clauseHash)}
          </span>
          <span className="text-xs text-slate-400">
            &sect;{entry.sectionReference}
          </span>
        </div>
      </td>
      <td className="px-4 py-2.5">
        <StatusBadge status={entry.evaluationResult} />
        {entry.hitlReviewId && (
          <span className="ml-1.5 text-[11px] text-slate-400">
            {entry.hitlReviewId}
          </span>
        )}
      </td>
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-1 font-mono text-xs text-slate-400">
          <span title={entry.previousHash}>
            {truncateHash(entry.previousHash)}
          </span>
          <span className="text-slate-300">&rarr;</span>
          <span className="text-slate-600" title={entry.currentHash}>
            {truncateHash(entry.currentHash)}
          </span>
        </div>
      </td>
    </tr>
  );
}
