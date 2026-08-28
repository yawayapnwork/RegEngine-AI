import { UploadCloud } from "lucide-react";
import { useRef, useState } from "react";

export default function PdfUploadZone({ onFileSelected }) {
  const inputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);

  const handleFiles = (files) => {
    const file = files?.[0];
    if (file && file.type === "application/pdf") onFileSelected?.(file);
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setIsDragging(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => inputRef.current?.click()}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
        isDragging
          ? "border-sky-500 bg-sky-500/5"
          : "border-ink-700 bg-ink-900/40 hover:border-ink-600"
      }`}
    >
      <UploadCloud
        className={`h-8 w-8 ${isDragging ? "text-sky-400" : "text-slate-500"}`}
      />
      <p className="mt-3 text-sm font-medium text-slate-300">
        Drop a SEBI Master Circular PDF, or click to browse
      </p>
      <p className="mt-1 text-xs text-slate-500">
        Feeds POST /v1/circulars/parse-and-index &middot; max 50MB
      </p>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        onChange={(e) => handleFiles(e.target.files)}
      />
    </div>
  );
}
