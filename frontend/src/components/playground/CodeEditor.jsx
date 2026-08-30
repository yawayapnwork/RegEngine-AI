import { useRef } from "react";

/** A minimal, dependency-free line-numbered code editor: a `<textarea>`
 * with a synced line-number gutter. Deliberately not a Monaco/CodeMirror
 * integration -- this project's frontend has zero editor dependencies
 * today (see frontend/package.json), and a full editor's syntax-highlight
 * grammar for Rego doesn't exist as an off-the-shelf package worth
 * pulling in for one playground view. Everything needed for
 * Requirement 1 (a live-editable code panel) works without it. */
export default function CodeEditor({ value, onChange, readOnly = false, placeholder, ariaLabel, tone = "default" }) {
  const gutterRef = useRef(null);
  const lineCount = Math.max(1, value.split("\n").length);

  const syncScroll = (e) => {
    if (gutterRef.current) gutterRef.current.scrollTop = e.target.scrollTop;
  };

  const toneClasses = tone === "error" ? "text-red-600" : "text-slate-800";

  return (
    <div className="flex h-full overflow-hidden bg-ink-850 font-mono text-[13px] leading-relaxed">
      <pre
        ref={gutterRef}
        aria-hidden="true"
        className="select-none overflow-hidden px-3 py-3 text-right text-slate-400"
      >
        {Array.from({ length: lineCount }, (_, i) => i + 1).join("\n")}
      </pre>
      <textarea
        aria-label={ariaLabel}
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
        onScroll={syncScroll}
        readOnly={readOnly}
        placeholder={placeholder}
        spellCheck={false}
        wrap="off"
        className={`flex-1 resize-none overflow-auto whitespace-pre bg-transparent px-3 py-3 outline-none placeholder:text-slate-400 ${toneClasses} ${
          readOnly ? "cursor-default" : ""
        }`}
      />
    </div>
  );
}
