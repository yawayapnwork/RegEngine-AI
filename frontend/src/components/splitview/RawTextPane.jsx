const KIND_CLASSES = {
  threshold: "bg-sky-500/20 text-sky-300 ring-1 ring-sky-500/40",
  condition: "bg-violet-500/20 text-violet-300 ring-1 ring-violet-500/40",
  qualitative: "bg-amber-500/20 text-amber-300 ring-1 ring-amber-500/40",
};

/** Splits `[[marked]]` spans out of rawText and pairs each, in order, with
 * the matching entry in `highlights` so click/hover state can drive both
 * this pane and RegoPane from one shared `activeIndex`. */
function segments(rawText) {
  return rawText.split(/(\[\[[^\]]+\]\])/g).map((chunk, i) => {
    const match = chunk.match(/^\[\[([^\]]+)\]\]$/);
    return match
      ? { text: match[1], marked: true, key: i }
      : { text: chunk, marked: false, key: i };
  });
}

export default function RawTextPane({ clause, activeIndex, onSelect }) {
  let highlightCursor = -1;

  return (
    <div className="prose-invert max-w-none whitespace-pre-wrap text-[15px] leading-relaxed text-slate-300">
      {segments(clause.rawText).map((seg) => {
        if (!seg.marked) return <span key={seg.key}>{seg.text}</span>;

        highlightCursor += 1;
        const index = highlightCursor;
        const highlight = clause.highlights[index];
        const isActive = activeIndex === index;

        return (
          <button
            key={seg.key}
            onClick={() => onSelect(isActive ? null : index)}
            className={`mx-0.5 rounded px-1 py-0.5 font-medium transition-all ${KIND_CLASSES[highlight?.kind] || "bg-slate-500/20"} ${
              isActive
                ? "outline outline-2 outline-offset-1 outline-white/60"
                : ""
            }`}
            title={`clause element: ${highlight?.kind}`}
          >
            {seg.text}
          </button>
        );
      })}
    </div>
  );
}
