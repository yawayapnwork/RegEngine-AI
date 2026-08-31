import { UploadCloud } from "lucide-react";
import { useRef, useState } from "react";

export default function PdfUploadZone({ onFileSelected, disabled = false }) {
  const inputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);

  const handleFiles = (files) => {
    if (disabled) return;
    const file = files?.[0];
    if (file && file.type === "application/pdf") onFileSelected?.(file);
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setIsDragging(false);
        handleFiles(e.dataTransfer.files);
      }}
      onClick={() => !disabled && inputRef.current?.click()}
      className={`flex items-center gap-3 rounded-sm border px-4 py-3 transition-colors ${
        disabled
          ? "cursor-not-allowed border-dashed border-ink-700 bg-ink-850 opacity-60"
          : "cursor-pointer border-dashed border-ink-700 bg-ink-900 hover:border-ink-650"
      } ${isDragging && !disabled ? "border-blue-400 bg-blue-50" : ""}`}
    >
      <UploadCloud
        className={`h-4 w-4 shrink-0 ${isDragging && !disabled ? "text-blue-600" : "text-slate-400"}`}
      />
      <p className="text-sm text-slate-700">
        {disabled ? "Upload in progress..." : "Drop a SEBI Master Circular PDF, or click to browse"}
      </p>
      <p className="ml-auto font-mono text-2xs text-slate-400">
        POST /v1/ingestion/uploads &middot; max 50MB
      </p>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        disabled={disabled}
        onChange={(e) => handleFiles(e.target.files)}
      />
    </div>
  );
}
