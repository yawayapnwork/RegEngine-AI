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
      className={`flex cursor-pointer items-center gap-3 rounded-sm border px-4 py-3 transition-colors ${
        isDragging
          ? "border-blue-400 bg-blue-50"
          : "border-dashed border-ink-700 bg-ink-900 hover:border-ink-650"
      }`}
    >
      <UploadCloud
        className={`h-4 w-4 shrink-0 ${isDragging ? "text-blue-600" : "text-slate-400"}`}
      />
      <p className="text-sm text-slate-700">
        Drop a SEBI Master Circular PDF, or click to browse
      </p>
      <p className="ml-auto font-mono text-2xs text-slate-400">
        POST /v1/circulars/parse-and-index &middot; max 50MB
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
