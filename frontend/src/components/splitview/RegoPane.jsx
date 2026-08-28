import { ShieldOff } from "lucide-react";

export default function RegoPane({ clause, activeIndex }) {
  if (!clause.regoCode) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center text-slate-500">
        <ShieldOff className="h-8 w-8" />
        <p className="text-sm font-medium text-slate-400">
          No compiled Rego for this clause
        </p>
        <p className="text-xs">
          Blocked by HITL &mdash; qualitative directives are not reducible to
          deterministic policy code. See the HITL Compliance Review tab.
        </p>
      </div>
    );
  }

  const activeAnchor =
    activeIndex != null ? clause.highlights[activeIndex]?.regoAnchor : null;

  return (
    <pre className="whitespace-pre text-[13px] leading-relaxed text-slate-300">
      <code>
        {clause.regoCode.split("\n").map((line, i) => {
          const isActive = activeAnchor && line.includes(activeAnchor);
          return (
            <div
              key={i}
              className={`px-3 ${isActive ? "-mx-3 bg-sky-500/10 ring-1 ring-inset ring-sky-500/30" : ""}`}
            >
              <span className="mr-4 inline-block w-6 select-none text-right text-slate-600">
                {i + 1}
              </span>
              {line || " "}
            </div>
          );
        })}
      </code>
    </pre>
  );
}
