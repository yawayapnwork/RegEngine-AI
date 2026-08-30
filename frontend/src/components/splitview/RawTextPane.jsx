const KIND_CLASSES = {
  threshold: "bg-blue-50 text-blue-800 border-b-2 border-blue-400",
  condition: "bg-violet-50 text-violet-800 border-b-2 border-violet-400",
  qualitative: "bg-amber-50 text-amber-800 border-b-2 border-amber-400",
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
    <div className="max-w-none whitespace-pre-wrap text-[15px] leading-relaxed text-slate-700">
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
            className={`mx-0.5 px-0.5 py-0.5 font-medium transition-colors ${KIND_CLASSES[highlight?.kind] || "bg-slate-100 border-b-2 border-slate-400"} ${
              isActive ? "bg-slate-900/10 text-slate-900" : ""
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
